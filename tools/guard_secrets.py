"""Refuse to commit credentials. Runs as a pre-commit hook.

The API keys here are billable (Gemini, Anthropic) and the Paperless token opens
a server holding the family's medical records. A key pushed to a public repo is
scraped within minutes -- treat any key that lands in a commit as burned, and
rotate it rather than reaching for a history rewrite.

Detects, in the STAGED content only:

  * known key shapes, which are unambiguous and cheap to match: Google/Gemini
    (AIza...), Anthropic (sk-ant-...), OpenAI (sk-...), GitHub (ghp_/gho_/ghu_/
    ghs_/ghr_/github_pat_), AWS (AKIA...), Slack (xox[bapsr]-), and PEM private
    key blocks.
  * assignments that smell like a credential -- `api_key = "..."`, `password:
    "..."`, `token = '...'` -- with a value long enough to be real.

Placeholders are allowed, because the example configs and the docs need them. A
value is treated as a placeholder if it is obviously one ("...", "xxx", "your-key
-here", "changeme", "example", "<...>") or is too short to be a real secret.

This is a floor, not a ceiling. It will not catch a high-entropy string with no
recognisable shape sitting in a config file. `secrets/` is gitignored and
tools/guard_pii.py refuses to stage anything under it; this guard is what catches
a key pasted into a source file, a docstring, or a test.

    python -m tools.guard_secrets          # checks staged content
    python -m tools.guard_secrets --all    # checks the whole working tree
"""

from __future__ import annotations

import re
import subprocess
import sys

# Shapes that are a credential and nothing else.
KEY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Google / Gemini API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[0-9A-Za-z_\-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?!ant-)[0-9A-Za-z]{32,}\b")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{60,})\b")),
    ("AWS access key id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[bapsr]-[0-9A-Za-z\-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.")),
]

# `password = "hunter2"`, `"api_key": "abc..."`, `token: abc...`
ASSIGNMENT = re.compile(r"""(?ix)
    \b (api[_\-]?key | apikey | secret | password | passwd | pwd | token
       | auth[_\-]?token | access[_\-]?token | client[_\-]?secret | private[_\-]?key)
    \b \s* [:=] \s*
    ["']? (?P<value> [^\s"',;]{8,} ) ["']?
    """)

# A value that is plainly not a real secret. The example configs and the README
# are FULL of these, and a guard that cries wolf gets switched off.
PLACEHOLDER = re.compile(r"""(?ix)
    ^( \.{2,} | x{3,} | \*{3,} | -+ | none | null | true | false | \d+
     | .*(your[_\-]?|my[_\-]?|the[_\-]?)?(key|token|secret|password|here|goes)
     | .*(example|placeholder|changeme|change[_\-]?me|dummy|fake|sample|test|todo|fixme)
     | .*(\.\.\.|<.*>|\{.*\}|\$\{.*\}|%\(.*\)s)
     )$
    """)


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [f for f in out.stdout.splitlines() if f.strip()]


def all_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [f for f in out.stdout.splitlines() if f.strip()]


def staged_content(path: str) -> str:
    out = subprocess.run(["git", "show", f":{path}"], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


# `password = os.environ.get("NALAM_PAPERLESS_PASSWORD", "")` is the CORRECT way to
# handle a password, and an early version of this guard flagged it. A secret that
# has leaked is a literal; a call, a subscript or an attribute lookup is code that
# goes and fetches one. Flagging the right pattern is how a guard gets switched off.
CODE_CHARS = set("()[]{}")
CODE_PREFIXES = ("os.", "self.", "cls.", "config.", "settings.", "json.", "env.", "getenv")


def looks_like_code(value: str) -> bool:
    return bool(CODE_CHARS & set(value)) or value.startswith(CODE_PREFIXES)


def looks_like_a_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER.match(value)) or len(set(value)) <= 3


# The escape hatch, and it is deliberately a bad one to reach for: it is per LINE,
# never per file. A file-level exemption is a hole that grows -- one real key added
# to an exempted file is invisible forever. A line-level one has to be written out
# next to the thing it excuses, where a reviewer sees it.
#
# tests/test_guards.py is the only legitimate user: it holds fabricated credentials
# with the SHAPE of the real thing, because that is the only part the guard can see,
# and a guard tested against fakes that do not look real is not tested.
ALLOWLIST = re.compile(r"pragma:\s*allowlist\s+secret", re.IGNORECASE)


def scan(path: str, text: str) -> list[str]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if ALLOWLIST.search(line):
            continue
        for label, pattern in KEY_PATTERNS:
            if pattern.search(line):
                hits.append(f"  {path}:{n}\n      looks like a {label}.")

        m = ASSIGNMENT.search(line)
        if (
            m
            and not looks_like_a_placeholder(m.group("value"))
            and not looks_like_code(m.group("value"))
        ):
            hits.append(
                f"  {path}:{n}\n"
                f"      a credential-shaped assignment with a real-looking value.\n"
                f'      If it is a placeholder, make it obviously one ("...", "<your-key>").'
            )
    return hits


def main() -> int:
    check_all = "--all" in sys.argv
    files = all_files() if check_all else staged_files()

    failures: list[str] = []
    for path in files:
        if path.endswith((".png", ".jpg", ".jpeg", ".pdf", ".gif", ".ico", ".db", ".pyc")):
            continue
        text = (
            open(path, encoding="utf-8", errors="ignore").read()
            if check_all
            else staged_content(path)
        )
        if text:
            failures.extend(scan(path, text))

    if failures:
        print("\nguard_secrets: REFUSING TO COMMIT\n", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        print(
            "\nKeys in a public repository are scraped within minutes. If one of these is\n"
            "a live credential, ROTATE IT -- do not try to rewrite it out of history.\n"
            "Real keys belong in secrets/, which is gitignored.\n",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
