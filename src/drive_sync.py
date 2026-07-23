"""Walk each person's folder and file every document into Paperless.

Path carries the metadata, so nothing has to be inferred from content:

    {directory}/{Specialty}/{YYYY-MM-DD} - {Title}.pdf
        |         |           |             `- title
        |         |           `- document date
        |         `- tag        (data/specialties.json)
        `- each person names their own folder EXPLICITLY in data/people.json
           ("directory", a full path). The patient is that folder, not the
           folder's *name* -- names drift (a rename, a trailing space, a
           duplicate copy) and a document filed against the wrong family member
           is the worst failure this system can have. We walk the exact paths
           people.json lists and nothing else: there is no shared root, none is
           assumed, and folders belonging to no listed person are simply not
           looked at. A new family member is added by editing people.json.

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

from src import config
from src.constants import (
    CONSUMABLE_EXT,
    DOCUMENT_TYPE,
    MAGIC,
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


def collect() -> tuple[list[Doc], list[str]]:
    """Resolve every consumable file for every person. Returns (docs, skipped).

    Each person names the exact folder that holds their documents (`directory`
    in people.json); we walk those folders and nothing else. There is no shared
    root and none is assumed -- a person's folder can live anywhere on disk. The
    flow is deliberately small: read the config, walk each listed folder, hand
    back every consumable file. What is already in the database (and so skipped)
    is decided downstream, by source_path, not here.

    `rel` is the file's path within its person's folder, prefixed with the
    person's people.json KEY (`Perumal/Cardiology/2024-... .pdf`), not the
    folder's on-disk name. The key is the stable identity persisted as
    documents.source_path and the sync state key; the `directory` only says
    where to find the files today, so renaming the folder (Perumal -> "Perumal
    M") never re-keys a document or re-triggers extraction. No common root is
    used or assumed.
    """
    people = config.load(PEOPLE_CONFIG)
    specialties = config.load(SPECIALTIES_CONFIG)

    person_dirs: dict[str, str] = {
        key: os.path.expanduser(entry["directory"]) for key, entry in people["people"].items()
    }
    correspondents: dict[str, str] = {
        key: entry["correspondent"] for key, entry in people["people"].items()
    }
    # A sub-folder organised around an EVENT rather than a patient -- a
    # pregnancy -- collects the PARENTS' records under the child's name. Keys are
    # paths RELATIVE TO THE PERSON'S OWN directory ("Conception/...", not the
    # top-level folder name), so they survive a rename of that folder too.
    # Longest prefix wins.
    overrides: dict[str, str] = people.get("folder_overrides", {})
    ordered_overrides = sorted(overrides.items(), key=lambda kv: -len(kv[0]))
    tag_map: dict[str, str] = specialties["tags"]
    default_tag: str = specialties["default_tag"]

    docs: list[Doc] = []
    skipped: list[str] = []

    for key, person_dir in person_dirs.items():
        if not os.path.isdir(person_dir):
            raise RuntimeError(
                f"Directory for {key} does not exist: {person_dir}. "
                f'Fix its "directory" in {PEOPLE_CONFIG}.'
            )

        for dirpath, dirnames, filenames in os.walk(person_dir):
            sub = os.path.relpath(dirpath, person_dir)  # "." at the person's root
            sub = "" if sub == "." else sub
            parts = sub.split(os.sep) if sub else []

            dir_correspondent = correspondents[key]
            for prefix, who in ordered_overrides:
                if sub == prefix or sub.startswith(prefix + os.sep):
                    dir_correspondent = who
                    break

            # First path segment under the person that names a known specialty.
            tag = default_tag
            for part in parts:
                if part.strip() in tag_map:
                    tag = tag_map[part.strip()]
                    break

            for name in filenames:
                path = os.path.join(dirpath, name)
                suffix = sniff(path)
                file_sub = os.path.relpath(path, person_dir)
                rel = os.path.join(key, file_sub)
                if suffix is None:
                    skipped.append(rel)
                    continue

                # An override may name an exact FILE, not just a folder: a
                # handwritten form with a blank name field gives the document
                # nothing to override the folder with, so the folder's default
                # would silently take it. File override keys are person-relative.
                correspondent = dir_correspondent
                for prefix, who in ordered_overrides:
                    if file_sub == prefix:
                        correspondent = who
                        break

                stem = os.path.splitext(name)[0]
                title, created = parse_name(stem, os.path.basename(dirpath))
                docs.append(
                    Doc(
                        path=path,
                        rel=rel,
                        person=key,
                        correspondent=correspondent,
                        tag=tag,
                        title=title,
                        created=created,
                        suffix=suffix,
                        key=_key(path, rel),
                    )
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
                filename=os.path.basename(d.rel)
                + ("" if d.rel.lower().endswith(d.suffix) else d.suffix),
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
