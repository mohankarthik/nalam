"""LAN-only web UI: confirm medications, browse observation trends and
encounters, for whoever in the family opens it. No login -- same trust model
as opening data/health.db directly (see CLAUDE.md and the plan this was
built from).

Read-side logic is not reinvented here: routes.py calls the same functions
the Telegram Q&A bot uses (src/qa.py) and the same decision functions the CLI
tools use (src/meds.py) -- a confirmation clicked here and one typed on the
CLI go through identical code.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_HERE = os.path.dirname(__file__)

app = FastAPI(title="nalam")
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

from src.webapp import routes  # noqa: E402  (registers routes on import)

app.include_router(routes.router)
