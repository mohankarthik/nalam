"""Minimal Paperless-ngx REST client.

Uploads go through the API rather than the consume directory because the
consume directory cannot set metadata: correspondent (= the patient), tags and
the document date all have to be attached at post time.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import requests

from src.constants import PAPERLESS_URL, SECRETS_DIR

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 120


class PaperlessError(RuntimeError):
    pass


def load_credentials() -> tuple[str, str]:
    """Read the Paperless admin credentials.

    Paperless' REST API takes HTTP Basic auth, so nalam reuses the same admin
    login the Homepage widget already uses (``vault_admin_password``, ansible
    vault) rather than minting an API token. One credential, one place to
    rotate it.

    From secrets/paperless.json ({"username": ..., "password": ...}) or the
    NALAM_PAPERLESS_USER / NALAM_PAPERLESS_PASSWORD env vars that ansible
    renders into docker-compose.env.
    """
    path = os.path.join(SECRETS_DIR, "paperless.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            creds = json.load(f)
        if creds.get("username") and creds.get("password"):
            return str(creds["username"]), str(creds["password"])

    user = os.environ.get("NALAM_PAPERLESS_USER", "")
    password = os.environ.get("NALAM_PAPERLESS_PASSWORD", "")
    if not (user and password):
        raise PaperlessError(
            f"No Paperless credentials. Write {path} as "
            '{"username": "...", "password": "..."}, or set '
            "NALAM_PAPERLESS_USER / NALAM_PAPERLESS_PASSWORD."
        )
    return user, password


class Paperless:
    def __init__(self, url: str = PAPERLESS_URL, auth: Optional[tuple[str, str]] = None) -> None:
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = auth or load_credentials()
        self.session.headers["Accept"] = "application/json"
        # ocr_index() and document_id_index() both walk every correspondent and
        # every document Paperless holds -- a run that calls both (run_extract.py's
        # prescription/radiology passes do) would otherwise page through the same
        # few hundred documents twice. Cached per-instance, not across instances:
        # a long-lived Paperless() would go stale against new uploads.
        self._correspondents: Optional[dict[int, str]] = None
        self._documents: Optional[list[dict[str, Any]]] = None

    def _get_all(self, endpoint: str) -> list[dict[str, Any]]:
        """GET every page of a list endpoint."""
        results: list[dict[str, Any]] = []
        url: Optional[str] = f"{self.url}/api/{endpoint}/?page_size=250"
        while url:
            resp = self.session.get(url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            results.extend(body["results"])
            url = body.get("next")
        return results

    def _resolve(self, endpoint: str, name: str, create: bool) -> int:
        """Return the id of a named object, optionally creating it."""
        for obj in self._get_all(endpoint):
            if obj["name"] == name:
                return int(obj["id"])
        if not create:
            raise PaperlessError(f"No {endpoint} named {name!r} in Paperless")
        resp = self.session.post(
            f"{self.url}/api/{endpoint}/", json={"name": name}, timeout=HTTP_TIMEOUT
        )
        resp.raise_for_status()
        logger.info(f"Created {endpoint}: {name}")
        return int(resp.json()["id"])

    def correspondent_id(self, name: str, create: bool = False) -> int:
        # Correspondent == patient. Never auto-create: an unexpected name here
        # means the folder->person map is wrong, and a document filed against
        # the wrong person is the worst failure this system has.
        return self._resolve("correspondents", name, create)

    def tag_id(self, name: str, create: bool = True) -> int:
        return self._resolve("tags", name, create)

    def document_type_id(self, name: str, create: bool = True) -> int:
        return self._resolve("document_types", name, create)

    def upload(
        self,
        content: bytes,
        filename: str,
        title: str,
        correspondent: int,
        document_type: int,
        tags: list[int],
        created: Optional[str],
    ) -> str:
        """Post a document. Returns Paperless' consume task id.

        Consumption is asynchronous, and Paperless rejects a document whose
        checksum it already holds -- which is how the 11 insurance PDFs already
        in Paperless get skipped without us tracking them.
        """
        data: list[tuple[str, Any]] = [
            ("title", title),
            ("correspondent", str(correspondent)),
            ("document_type", str(document_type)),
        ]
        data.extend(("tags", str(t)) for t in tags)
        if created:
            data.append(("created", created))

        resp = self.session.post(
            f"{self.url}/api/documents/post_document/",
            data=data,
            files={"document": (filename, content)},
            timeout=HTTP_TIMEOUT,
        )
        if not resp.ok:
            raise PaperlessError(f"Upload of {filename!r} failed: {resp.text[:300]}")
        return str(resp.json())

    def _document_rows(self) -> tuple[dict[int, str], list[dict[str, Any]]]:
        """(correspondent names by id, every document), fetched once and cached.

        Both ocr_index() and document_id_index() need the exact same walk --
        every correspondent, every document -- differing only in which field of
        `doc` they keep. Caching means calling both in one run (as
        run_extract.py's prescription/radiology passes do) pages through
        Paperless once, not twice.
        """
        if self._correspondents is None:
            self._correspondents = {c["id"]: c["name"] for c in self._get_all("correspondents")}
        if self._documents is None:
            self._documents = self._get_all("documents")
        return self._correspondents, self._documents

    def _keyed_index(self, value_of, kind: str) -> dict[tuple[str, str], Any]:
        """{(correspondent, folded filename) -> value_of(doc)} for every document
        where that key is unique.

        Keyed on (correspondent, original filename), NOT on (title, date):
        Paperless rewrites `created` from its own date detection, and a title
        like "Prescription" occurs seven times across different dates. Joining
        on an ambiguous key could link to (or corroborate against) SOMEBODY
        ELSE's page -- a safety bug, not a coverage one -- so a key that is not
        unique is DROPPED rather than guessed at.
        """
        correspondents, documents = self._document_rows()

        seen: dict[tuple[str, str], int] = {}
        index: dict[tuple[str, str], Any] = {}
        for doc in documents:
            key = (
                correspondents.get(doc["correspondent"], ""),
                fold_filename(doc.get("original_file_name") or ""),
            )
            seen[key] = seen.get(key, 0) + 1
            value = value_of(doc)
            if value:
                index[key] = value

        ambiguous = {k for k, n in seen.items() if n > 1}
        for key in ambiguous:
            index.pop(key, None)
        if ambiguous:
            logger.info(
                f"{len(ambiguous)} documents share a (person, filename) key; dropped "
                f"from the {kind} index rather than risk linking to the wrong page."
            )
        return index

    def document_id_index(self) -> dict[tuple[str, str], int]:
        """{(correspondent, filename) -> Paperless document id} for every document.

        This is how `documents.paperless_id` gets filled in: extraction reads
        PDFs from the Drive mount, never from Paperless, so it has no id of its
        own until it looks one up here.
        """
        return self._keyed_index(lambda doc: doc["id"], kind="id")

    def ocr_index(self) -> dict[tuple[str, str], str]:
        """{(correspondent, filename) -> OCR text} for every document Paperless holds.

        Paperless OCR'd every document with Tesseract at consume time. For a
        scanned PDF -- which has no text layer of its own -- that is the only
        independent reading of the page we have, and without an independent
        reading a vision model's output cannot be checked at all. See src/oracle.
        """
        return self._keyed_index(lambda doc: (doc.get("content") or "").strip(), kind="OCR")


def fold_filename(name: str) -> str:
    """Normalise a filename for joining.

    The extensionless source PDFs were uploaded with '.pdf' appended, and some
    names carry doubled or trailing spaces. Neither changes which document it is.
    """
    stem = os.path.splitext((name or "").strip())[0]
    return re.sub(r"\s+", " ", stem).strip().lower()


def _index_lookup(index: dict[tuple[str, str], Any], correspondent: str, rel_path: str) -> Any:
    """One source document's value from an (correspondent, filename)-keyed index,
    or None if unresolved (not uploaded yet, or an ambiguous filename dropped
    from the index). Shared by ocr_for() and id_for() -- same key, same join."""
    return index.get((correspondent, fold_filename(os.path.basename(rel_path))))


def ocr_for(index: dict[tuple[str, str], str], correspondent: str, rel_path: str) -> Optional[str]:
    """The OCR text for one source document, or None if there is no independent reading."""
    return _index_lookup(index, correspondent, rel_path)


def id_for(index: dict[tuple[str, str], int], correspondent: str, rel_path: str) -> Optional[int]:
    """The Paperless document id for one source document, or None if unresolved."""
    return _index_lookup(index, correspondent, rel_path)
