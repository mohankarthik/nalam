"""The pre-commit guards, which are the last thing standing between a family's
medical history and a public repository.

They are worth testing for the same reason they exist: this repository already
published real patient names once, in comments and in test data, and nobody
noticed until the day it was about to go public. A guard nobody has tested is a
guard nobody should trust.

Two failure modes, and the second is the one that kills guards:

  * a MISS lets a secret or a name through.
  * a FALSE POSITIVE on correct code (`password = os.environ.get(...)`) makes the
    hook noise, and a noisy hook gets bypassed with --no-verify. Then it protects
    nothing at all.

Both are tested.
"""

from __future__ import annotations

import pytest

from tools.guard_secrets import scan

# Fabricated, structurally-valid credentials. None of these is real -- but each has
# the SHAPE of the real thing, which is the only part the guard can see, and a guard
# tested against fakes that do not look real is not tested.
#
# Which means this file trips its own guard. Each line therefore carries an explicit
# `pragma: allowlist secret`. That pragma is per-line by design: a file-level
# exemption would let a genuine key hide here forever.
FAKE_CREDENTIALS = {
    "google/gemini": 'K = "AIzaSyC1nQmVQ3xK9pLmZ7wRtY2bN4vH8jD6fA0"',  # pragma: allowlist secret
    "anthropic": 'key = "sk-ant-api03-abcdefghijklmnopqrst"',  # pragma: allowlist secret
    "openai": 'key = "sk-' + "A" * 40 + '"',  # pragma: allowlist secret
    "github": "token = 'ghp_" + "a" * 36 + "'",  # pragma: allowlist secret
    "aws": 'AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"',  # pragma: allowlist secret
    "slack": 'hook = "xoxb-123456789012-abcdefghijkl"',  # pragma: allowlist secret
    "private key": "-----BEGIN RSA PRIVATE KEY-----",  # pragma: allowlist secret
    "jwt": 'j = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc"',  # pragma: allowlist secret
    "plain password": 'pwd = "hunter2istoolong"',  # pragma: allowlist secret
}


def test_the_allowlist_pragma_is_the_only_thing_saving_this_file() -> None:
    """If the pragma ever stops working, this file becomes a hole. Prove it is
    load-bearing: the same lines WITHOUT the pragma must all be caught."""
    for label, line in FAKE_CREDENTIALS.items():
        assert scan("x.py", line), f"{label} is no longer detected at all"
        assert not scan("x.py", line + "  # pragma: allowlist secret"), (
            f"the allowlist pragma did not suppress {label} -- this file's own "
            f"fixtures would block every commit"
        )


# Every one of these is CORRECT code or documentation, and an early version of this
# guard flagged the first one. Reading a credential from the environment is the
# right thing to do; saying so must not be an offence.
MUST_NOT_FIRE = {
    "env read": 'password = os.environ.get("NALAM_PAPERLESS_PASSWORD", "")',
    "dict lookup": 'creds.get("password")',
    "subscript": 'return str(creds["username"]), str(creds["password"])',
    "placeholder": 'api_key = "your-key-here"',
    "ellipsis": 'token = "..."',
    "docstring example": '{"username": "...", "password": "..."}',
    "changeme": 'password = "changeme"',
    "prose": "The Paperless token opens a server holding medical records.",
}


@pytest.mark.parametrize("label", sorted(FAKE_CREDENTIALS))
def test_a_credential_is_caught(label: str) -> None:
    assert scan("x.py", FAKE_CREDENTIALS[label]), f"{label} slipped through the secrets guard"


@pytest.mark.parametrize("label", sorted(MUST_NOT_FIRE))
def test_correct_code_is_not_flagged(label: str) -> None:
    assert not scan("x.py", MUST_NOT_FIRE[label]), (
        f"{label} was flagged. A guard that cries wolf gets bypassed with --no-verify, "
        f"and then it guards nothing."
    )


def test_only_the_licence_is_exempt_from_the_name_check() -> None:
    """LICENSE names the author as copyright holder, which is public by definition,
    and MIT's wording cannot carry a comment pragma. Nothing ELSE may be exempt --
    a source file on this list would be a permanent hiding place for a patient."""
    from tools.guard_pii import ALLOWED_PATHS

    assert ALLOWED_PATHS == {"LICENSE"}


def test_the_denylist_itself_can_never_be_committed() -> None:
    """The deny-list names real people. It is the one file whose leak would be
    exactly the thing the guard exists to prevent."""
    from tools.guard_pii import FORBIDDEN_PATHS

    assert any(p.search("data/pii_denylist.txt") for p in FORBIDDEN_PATHS)


@pytest.mark.parametrize(
    "path",
    [
        "secrets/paperless.json",
        "data/people.json",
        "data/settings.json",
        "data/health.db",
        "data/llm/some/cached.json",
        "tests/fixtures/golden.json",
        "NOTES.local.md",
    ],
)
def test_personal_paths_are_refused(path: str) -> None:
    """`git add -f` defeats .gitignore silently. This check does not consult it."""
    from tools.guard_pii import FORBIDDEN_PATHS

    assert any(p.search(path) for p in FORBIDDEN_PATHS), f"{path} would have been committable"
