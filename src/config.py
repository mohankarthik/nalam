"""Load config JSON, dropping inline documentation.

Our config files document their own fields inline: any object key whose name
starts with ``_`` is a human note explaining the field beside it, never data
(verified: nothing reads a ``_`` key). Rather than have every loader repeat the
same ``if not k.startswith("_")`` filter -- and one loader forget it, or strip
only the top level while nested comments (drugs.json has them) slip through --
the stripping is done by the stdlib parser itself.

``object_hook`` fires on every JSON object the decoder builds, at every depth
and inside arrays too, so a single one-line filter covers the whole tree for
free. There is no bespoke recursion to get wrong.
"""

from __future__ import annotations

import json
from typing import Any


def _drop_comments(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if not k.startswith("_")}


def load(path: str, encoding: str = "utf-8") -> Any:
    """json.load for a config file, with ``_``-prefixed comment keys removed."""
    with open(path, encoding=encoding) as f:
        return json.load(f, object_hook=_drop_comments)


def loads(text: str) -> Any:
    """json.loads with ``_``-prefixed comment keys removed."""
    return json.loads(text, object_hook=_drop_comments)
