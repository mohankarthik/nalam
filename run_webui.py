"""The web UI: confirm medications, browse observation trends and encounters.
LAN-only, no login -- see CLAUDE.md and docs on why that's fine here.

    python run_webui.py                 serve on 0.0.0.0:8000 (or data/settings.json's webui_port)
"""

from __future__ import annotations

import uvicorn

from src.constants import SETTINGS

if __name__ == "__main__":
    port = int(SETTINGS.get("webui_port", 8000))
    uvicorn.run("src.webapp.app:app", host="0.0.0.0", port=port)
