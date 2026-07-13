"""Walk the Drive Medical folder and file every document into Paperless.

Path carries the metadata, so nothing has to be inferred from content:

    Medical/{Person}/{Specialty}/{YYYY-MM-DD} - {Title}.pdf
            |         |           |             `- title
            |         |           `- document date
            |         `- tag        (data/specialties.json)
            `- correspondent = the patient  (data/people.json)

Idempotent two ways: a local state file skips what we already posted, and
Paperless rejects a duplicate checksum server-side. Safe to re-run.

State is keyed on path+size+mtime, deliberately not on a content hash: this
walks a Drive mount, and hashing every file each run would re-download the
whole folder on every cron tick. Correctness does not rest on the key -- a
false miss just re-posts a file that Paperless then rejects as a duplicate.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from src.constants import (
    CONSUMABLE_EXT,
    DOCUMENT_TYPE,
    MAGIC,
    MEDICAL_ROOT,
    PEOPLE_CONFIG,
    SPECIALTIES_CONFIG,
    STATE_DIR,
    SYNC_STATE,
)
from src.paperless import HTTP_TIMEOUT, Paperless

logger = logging.getLogger(__name__)

_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*-?\s*(.*)$")


@dataclass
class Doc:
    """One source file, resolved to its Paperless metadata."""

    path: str
    rel: str
    person: str
    correspondent: str
    tag: str
    title: str
    created: Optional[str]
    suffix: str
    key: str


def sniff(path: str) -> Optional[str]:
    """Return the file's real extension, or None if Paperless cannot consume it.

    Extension first, magic bytes only as a fallback. Both halves are load-bearing:
    40 source PDFs carry no extension at all, but this walks an rclone mount that
    fetches the whole object on open -- so opening all 529 files just to read 8
    bytes downloads the entire folder. Known extensions are trusted; only the
    unlabelled ones are read.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in CONSUMABLE_EXT:
        return ext
    if ext:
        return None  # a known-bad extension (xlsx, docx): no Tika container

    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as e:
        logger.warning(f"Unreadable, skipping: {path} ({e})")
        return None
    for magic, suffix in MAGIC.items():
        if head.startswith(magic):
            return suffix
    return None


def parse_name(stem: str, parent: str) -> tuple[str, Optional[str]]:
    """Split '2024-09-04 - ROP' into a title and an ISO date.

    Falls back to the parent directory's date when the file has none (a few
    documents live in dated leaf folders). Title falls back to the parent name
    for files that are only a date.
    """
    date: Optional[str] = None
    title = stem

    m = _DATE_PREFIX.match(stem)
    if m:
        date, title = m.group(1), m.group(2).strip()
    else:
        pm = _DATE_PREFIX.match(parent)
        if pm:
            date = pm.group(1)

    if not title:
        title = parent or stem

    if date:
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            date = None

    return title, date


def _key(path: str, rel: str) -> str:
    st = os.stat(path)
    return f"{rel}|{st.st_size}|{int(st.st_mtime)}"


def collect(root: str = MEDICAL_ROOT) -> tuple[list[Doc], list[str]]:
    """Resolve every consumable file under root. Returns (docs, skipped)."""
    with open(PEOPLE_CONFIG, encoding="utf-8") as f:
        people = json.load(f)
    with open(SPECIALTIES_CONFIG, encoding="utf-8") as f:
        specialties = json.load(f)

    folder_to_person: dict[str, str] = {
        folder: entry["correspondent"] for folder, entry in people["people"].items()
    }
    skip_folders: set[str] = set(people["skip_folders"])
    tag_map: dict[str, str] = specialties["tags"]
    default_tag: str = specialties["default_tag"]

    docs: list[Doc] = []
    skipped: list[str] = []
    unmapped: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in skip_folders]
            continue

        parts = rel_dir.split(os.sep)
        folder = parts[0]
        if folder in skip_folders:
            continue

        correspondent = folder_to_person.get(folder)
        if correspondent is None:
            unmapped.add(folder)
            continue

        # First path segment under the person that names a known specialty.
        tag = default_tag
        for part in parts[1:]:
            if part.strip() in tag_map:
                tag = tag_map[part.strip()]
                break

        for name in filenames:
            path = os.path.join(dirpath, name)
            suffix = sniff(path)
            if suffix is None:
                skipped.append(os.path.relpath(path, root))
                continue
            rel = os.path.relpath(path, root)
            stem = os.path.splitext(name)[0]
            title, created = parse_name(stem, os.path.basename(dirpath))
            docs.append(
                Doc(
                    path=path,
                    rel=rel,
                    person=folder,
                    correspondent=correspondent,
                    tag=tag,
                    title=title,
                    created=created,
                    suffix=suffix,
                    key=_key(path, rel),
                )
            )

    if unmapped:
        raise RuntimeError(
            "Unmapped person folders (refusing to guess a patient): "
            + ", ".join(sorted(unmapped))
            + f". Add them to {PEOPLE_CONFIG}."
        )

    return docs, skipped


def load_state() -> dict[str, str]:
    if os.path.exists(SYNC_STATE):
        with open(SYNC_STATE, encoding="utf-8") as f:
            return dict(json.load(f))
    return {}


def save_state(state: dict[str, str]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SYNC_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def report_failures() -> int:
    """List documents Paperless refused to consume. Returns the failure count.

    Consumption is asynchronous: `upload()` returning a task id only means
    Paperless accepted the POST, not that the document landed. Without this,
    a doc that fails OCR is recorded as synced and silently never retried.
    Duplicates are reported separately -- those are a success, not a failure
    (they mean the document was already in Paperless).
    """
    api = Paperless()
    resp = api.session.get(f"{api.url}/api/tasks/", timeout=HTTP_TIMEOUT)
    resp.raise_for_status()

    duplicates, failures = [], []
    for task in resp.json():
        if task.get("status") != "FAILURE":
            continue
        result = str(task.get("result", ""))
        name = str(task.get("task_file_name", "?"))
        (duplicates if "duplicate" in result.lower() else failures).append((name, result))

    logger.info(f"{len(duplicates)} already in Paperless (duplicate, expected).")
    if failures:
        logger.error(f"{len(failures)} FAILED to consume:")
        for name, result in failures:
            logger.error(f"  {name}: {result[:160]}")
    else:
        logger.info("No consume failures.")
    return len(failures)


def sync(dry_run: bool = False, limit: int = 0) -> None:
    docs, skipped = collect()
    state = load_state()
    todo = [d for d in docs if d.key not in state]

    logger.info(
        f"{len(docs)} consumable, {len(skipped)} unconsumable, "
        f"{len(docs) - len(todo)} already synced, {len(todo)} to upload."
    )
    for s in skipped:
        logger.info(f"  skipped (not a PDF/image): {s}")

    if limit:
        todo = todo[:limit]
    if dry_run:
        for d in todo:
            logger.info(
                f"  [{d.correspondent}] {d.tag} | {d.created or '(no date)'} | "
                f"{d.title} <- {d.rel}"
            )
        return
    if not todo:
        return

    api = Paperless()
    doc_type = api.document_type_id(DOCUMENT_TYPE)
    # Resolved once: every lookup is a full paginated fetch.
    corr_ids = {c: api.correspondent_id(c) for c in {d.correspondent for d in docs}}
    tag_ids = {t: api.tag_id(t) for t in {d.tag for d in docs}}

    for i, d in enumerate(todo, 1):
        with open(d.path, "rb") as f:
            content = f.read()
        try:
            task = api.upload(
                content=content,
                filename=os.path.basename(d.rel) + ("" if d.rel.lower().endswith(d.suffix) else d.suffix),
                title=d.title,
                correspondent=corr_ids[d.correspondent],
                document_type=doc_type,
                tags=[tag_ids[d.tag]],
                created=d.created,
            )
        except Exception as e:
            logger.error(f"[{i}/{len(todo)}] FAILED {d.rel}: {e}")
            continue
        state[d.key] = d.rel
        save_state(state)
        logger.info(f"[{i}/{len(todo)}] {d.correspondent} | {d.title} -> {task}")
