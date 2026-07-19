"""Paths and settings.

Everything personal -- where the documents live, who the people are, and (for
the deprecated one-time codebook import) which spreadsheet once seeded it --
comes from `data/settings.json` and `data/people.json`, both gitignored. The
code holds no names and no paths.
"""

from __future__ import annotations

import json
import os
from typing import Any

DATA_DIR = "data"
STATE_DIR = os.path.join(DATA_DIR, "state")
SECRETS_DIR = "secrets"

SETTINGS_CONFIG = os.path.join(DATA_DIR, "settings.json")
PEOPLE_CONFIG = os.path.join(DATA_DIR, "people.json")
SPECIALTIES_CONFIG = os.path.join(DATA_DIR, "specialties.json")
CONDITIONS_CONFIG = os.path.join(DATA_DIR, "conditions.json")
SYNC_STATE = os.path.join(STATE_DIR, "synced.json")


def _load_settings() -> dict[str, Any]:
    if not os.path.exists(SETTINGS_CONFIG):
        raise FileNotFoundError(
            f"{SETTINGS_CONFIG} not found. Copy data/settings.example.json to it "
            "and fill in your document root and people."
        )
    with open(SETTINGS_CONFIG, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


SETTINGS = _load_settings()

# The Drive folder holding the scanned documents, one sub-folder per person.
MEDICAL_ROOT = os.path.expanduser(os.environ.get("NALAM_MEDICAL_ROOT", SETTINGS["medical_root"]))

PAPERLESS_URL = os.environ.get(
    "NALAM_PAPERLESS_URL", SETTINGS.get("paperless_url", "http://localhost:8100")
)

# Every document nalam files is a medical one.
DOCUMENT_TYPE = SETTINGS.get("document_type", "Medical")

# Paperless without a Tika/Gotenberg container consumes PDFs and images only;
# Office files are skipped and reported, never silently dropped.
CONSUMABLE_EXT = {".pdf", ".jpg", ".jpeg", ".png"}

# Some source PDFs carry no extension at all, so those get sniffed by magic bytes.
MAGIC = {
    b"%PDF": ".pdf",
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG": ".png",
}
