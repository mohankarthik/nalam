"""Refuse to commit personal data. Runs as a pre-commit hook.

This repository is public. Anything committed is published, permanently: GitHub
caches and indexes commit contents, so a later force-push does not take it back.
And the thing being handled here is a family's medical history.

The rule in CLAUDE.md -- "do not paste a real name, a real value, or a real file
path into anything that gets committed" -- was already being broken when this
guard was written. A family member's name sat in test_patient_identity.py as
literal test data, and in comments in validator.py and ingest.py, next to a real
clinical fact about that person. It survived an earlier manual scrub. A rule that
depends on remembering it is not a rule; it is a hope.

Two checks, both on the STAGED content only:

  1. No file that .gitignore protects may be staged. `git add -f` defeats
     .gitignore silently, and health.db is one keystroke away from public.
  2. No staged text may contain a term from the deny-list.

THE DENY-LIST IS NOT IN THIS REPOSITORY. It names real people, so committing it
would be the exact thing it exists to prevent. It lives in the gitignored
data/pii_denylist.txt; data/pii_denylist.example.txt carries the format. If the
deny-list is missing the hook says so and fails -- silently passing because the
config is absent is how a guard becomes decoration.

    python -m tools.guard_pii            # checks staged content
    python -m tools.guard_pii --all      # checks the whole working tree
    python -m tools.guard_pii --msg FILE # checks a commit message

THE COMMIT MESSAGE IS PUBLISHED TOO. That is not obvious, and it nearly went
wrong: a commit describing this very guard quoted a real patient's printed name to
explain the bug it fixed. The staged files were spotless. The message was not, and
`git log` on a public repository is as public as the code.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

DENYLIST = os.path.join("data", "pii_denylist.txt")

# Paths that must never be committed, whatever .gitignore is doing. This
# duplicates .gitignore on purpose: `git add -f` bypasses .gitignore, and this
# check does not care what git thinks.
FORBIDDEN_PATHS = [
    re.compile(p)
    for p in (
        r"^secrets/",
        r"^data/settings\.json$",
        r"^data/people\.json$",
        # data/analytes.json is committed since the codebook consolidation: it is
        # LOINC-keyed generic medical knowledge (per-sex ranges, no names/values/
        # paths). It is no longer path-forbidden, but its CONTENT is still scanned
        # against the deny-list below, so a real name landing in it is still caught.
        r"^data/pii_denylist\.txt$",
        r"^data/health\.db",
        r"^data/state/",
        r"^data/llm/",
        r"^tests/fixtures/(golden|sheet_errors|extracted|golden_set)",
        r"^NOTES\.local\.md$",
        r"\.bak$",
    )
]

SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".pdf", ".gif", ".ico", ".db", ".pyc")

# The author's own name, as the copyright holder, is his public identity -- and the
# MIT text is fixed legal wording that cannot carry a comment pragma. So LICENSE is
# exempt, and ONLY LICENSE.
#
# The deny-list still protects that same name everywhere else, which is the point:
# the author's name was ALSO sitting in test_patient_identity.py as PATIENT data,
# and a patient is not an author. Dropping the name from the deny-list to make the
# licence pass would have reopened the exact hole this guard was written to close.
#
# (This comment used to quote the name, to make the example concrete. The guard
# refused the commit. It was right.)
ALLOWED_PATHS = {"LICENSE"}


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
    """The content as STAGED, not as on disk -- they differ, and the staged
    version is the one about to be committed."""
    out = subprocess.run(["git", "show", f":{path}"], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def load_denylist() -> list[str]:
    if not os.path.exists(DENYLIST):
        print(f"guard_pii: {DENYLIST} is missing.", file=sys.stderr)
        print(
            "  It holds the real names this repository must never publish, which is why\n"
            "  it is gitignored rather than committed. Copy data/pii_denylist.example.txt\n"
            "  to it and fill it in. Refusing to run without it: a guard that passes when\n"
            "  its config is absent protects nothing.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    terms = []
    with open(DENYLIST, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    return terms


def check_message(path: str, terms: list[str]) -> list[str]:
    """A commit message is published exactly as surely as the code is."""
    try:
        text = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return []

    # Strip the comment lines git puts in the editor template.
    body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))

    failures = []
    lowered = body.lower()
    for term in terms:
        if term.lower() in lowered:
            failures.append(
                f"  the commit message names {term!r}.\n"
                f"      `git log` on a public repository is as public as the code.\n"
                f"      Describe the bug with an invented name (Alice Doe, Bob Example)."
            )
    return failures


def main() -> int:
    if "--msg" in sys.argv:
        path = sys.argv[sys.argv.index("--msg") + 1]
        failures = check_message(path, load_denylist())
        if failures:
            print("\nguard_pii: REFUSING THIS COMMIT MESSAGE\n", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        return 0

    check_all = "--all" in sys.argv
    files = all_files() if check_all else staged_files()
    if not files:
        return 0

    terms = load_denylist()
    failures: list[str] = []

    for path in files:
        for pattern in FORBIDDEN_PATHS:
            if pattern.search(path):
                failures.append(
                    f"  {path}\n"
                    f"      This path holds personal data and must never be committed."
                )
                break

    for path in files:
        if path.endswith(SKIP_SUFFIXES) or path in ALLOWED_PATHS:
            continue
        text = (
            open(path, encoding="utf-8", errors="ignore").read()
            if check_all
            else staged_content(path)
        )
        if not text:
            continue
        lowered = text.lower()
        for term in terms:
            if term.lower() in lowered:
                for n, line in enumerate(text.splitlines(), 1):
                    if term.lower() in line.lower():
                        failures.append(
                            f"  {path}:{n}\n"
                            f"      contains a deny-listed term: {term!r}\n"
                            f"      Use an invented name (Alice Doe, Bob Example)."
                        )
                        break

    if failures:
        print("\nguard_pii: REFUSING TO COMMIT\n", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        print(
            "\nThis repository is public. A commit is permanent -- GitHub caches and\n"
            "indexes it, and a force-push later does not take it back.\n",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
