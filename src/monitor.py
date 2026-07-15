"""Paperless-ngx health monitoring + Uptime-Kuma push.

Kept separate from any "nalam pipeline alive" heartbeat: an outage in
Paperless has to page a human distinctly from "nalam's cron stopped running",
because run_extract_queue.py deliberately refuses to extract through a
Paperless outage (see docs/telegram_ingest_queue.md) rather than guessing --
silence there is safe only if someone notices and fixes Paperless itself.

Same push() shape as gajana's src/monitor.py: no-op if the push URL is unset,
never raises (a monitoring failure must not fail the pipeline).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from src.paperless import Paperless, PaperlessError

logger = logging.getLogger(__name__)

ENV_PAPERLESS_PUSH_URL = "NALAM_PAPERLESS_PUSH_URL"

_PUSH_TIMEOUT_SECONDS = 15
_PROBE_TIMEOUT_SECONDS = 15
_PROBE_RETRIES = 3
_PROBE_RETRY_BACKOFF_SECONDS = 2


def check_paperless() -> tuple[bool, str]:
    """Is Paperless reachable and authenticating right now?

    Deliberately cheap -- one page of one list endpoint, not a walk of every
    document -- so this can run every extract-queue tick, even the empty ones,
    without adding real load.

    NOT `/api/` itself: Paperless redirects that to `/api/schema/view/` (its
    OpenAPI schema view), which 406s on the plain `Accept: application/json`
    header every other call in this codebase already sends -- a false DOWN,
    not a real outage. `/api/correspondents/` is the same endpoint
    `Paperless._get_all()` already uses elsewhere, known to work.

    Retries here, not in Uptime-Kuma: this is a push monitor (nalam calls
    Kuma, not the other way around), so Kuma has no "retry before marking
    down" knob to configure -- that only exists for its own active monitors.
    A single flaky request otherwise pages a human for nothing.
    """
    try:
        paperless = Paperless()
    except PaperlessError as e:
        return False, f"credentials: {e}"

    last_error: Optional[str] = None
    for attempt in range(1, _PROBE_RETRIES + 1):
        try:
            resp = paperless.session.get(
                f"{paperless.url}/api/correspondents/",
                params={"page_size": 1},
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            return True, "OK"
        except requests.RequestException as e:
            last_error = f"unreachable: {e}"
            if attempt < _PROBE_RETRIES:
                logger.warning(f"Paperless probe attempt {attempt}/{_PROBE_RETRIES} failed: {e}")
                time.sleep(_PROBE_RETRY_BACKOFF_SECONDS * attempt)
    return False, last_error


def push(url: Optional[str], is_up: bool, msg: str) -> None:
    """Push a status heartbeat to an Uptime-Kuma push monitor.

    No-op when ``url`` is empty so local/dev runs don't need monitoring
    configured. Never raises -- a monitoring failure must not fail the
    pipeline it is watching.
    """
    if not url:
        logger.info(f"No Uptime-Kuma push URL configured; skipping. Status: {msg}")
        return
    status = "up" if is_up else "down"
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["status"] = status
    params["msg"] = msg
    full_url = urlunsplit(parts._replace(query=urlencode(params)))
    try:
        resp = requests.get(full_url, timeout=_PUSH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        logger.info(f"Pushed Uptime-Kuma status={status} msg={msg!r}")
    except requests.RequestException as e:
        logger.error(f"Failed to push Uptime-Kuma heartbeat: {e}")


def push_paperless(is_up: bool, msg: str) -> None:
    push(os.environ.get(ENV_PAPERLESS_PUSH_URL), is_up, msg)
