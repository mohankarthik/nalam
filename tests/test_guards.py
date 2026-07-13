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

# Fabricated credentials, ASSEMBLED AT RUNTIME. Every prefix is split from its body,
# so no contiguous key shape ever exists in this file on disk -- while the string the
# guard actually sees is complete, and exercises the real regex.
#
# The first version of this file did NOT do that. It held the fakes as plain literals
# and silenced the local guard with `# pragma: allowlist secret`. GitHub's secret
# scanner does not honour that pragma, flagged the fixture as a live Google API key
# within minutes of the push, and marked it publicly leaked. The key was fake and no
# real credential was ever exposed -- but a string that is structurally
# indistinguishable from a live key does not become safe because a comment nearby says
# it is. Providers scan, and some of them try the key.
#
# So: never write a realistic key literal, not even as a fixture, not even with a
# pragma. Split it.
_G, _A, _O, _H, _K, _S = "AIza", "sk-ant-", "sk-", "ghp_", "AKIA", "xox"

FAKE_CREDENTIALS = {
    "google/gemini": 'K = "' + _G + 'SyC1nQmVQ3xK9pLmZ7wRtY2bN4vH8jD6fA0"',
    "anthropic": 'key = "' + _A + 'api03-abcdefghijklmnopqrst"',
    "openai": 'key = "' + _O + "A" * 40 + '"',
    "github": "token = '" + _H + "a" * 36 + "'",
    "aws": 'AWS_KEY_ID = "' + _K + 'IOSFODNN7EXAMPLE"',
    "slack": 'hook = "' + _S + 'b-123456789012-abcdefghijkl"',
    "private key": "-----BEGIN RSA " + "PRIVATE KEY-----",
    "jwt": 'j = "ey' + "JhbGciOiJIUzI1NiJ9.ey" + 'JzdWIiOiIxMjM0NTY3ODkwIn0.abc"',
    # Split for the same reason as the rest: written whole, this line is itself a
    # credential-shaped assignment, and the guard rightly refuses it.
    "plain password": 'pwd = "hunter' + '2istoolong"',
}


def test_no_key_shape_is_written_literally_in_this_file() -> None:
    """The regression that matters. If someone 'tidies' the concatenations above back
    into plain literals, this file becomes a live-looking key on a public repo again."""
    import re

    source = open(__file__, encoding="utf-8").read()
    literal_key_shapes = [
        r"AIza[0-9A-Za-z_\-]{35}",
        r"sk-ant-[0-9A-Za-z_\-]{20,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bghp_[0-9A-Za-z]{36}\b",
    ]
    for shape in literal_key_shapes:
        assert not re.search(shape, source), (
            f"a literal matching {shape!r} is written out in this file. Split it: "
            f"GitHub's scanner reads the file, and it does not care that the key is fake."
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
