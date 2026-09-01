"""Loads `.env` into the environment, and resolves the optional provider pin.

`.env.example` has shipped since day one and the README pointed at it, but nothing
ever read it -- every provider calls `os.environ.get` directly. Copying the example
to `.env` and filling in a key therefore did nothing, and the failure was silent: the
chain fell through to NullProvider, the run still succeeded, and the report said
`llm provider: none` without ever mentioning the key it had ignored. See FAILURES.md
(2026-09-01).

Deliberately hand-rolled rather than taking python-dotenv as a dependency. The
zero-dependency-to-reach-the-LLM-tier stance in provider.py applies here for the same
reason, and the subset of the format that matters is small enough to read in one sitting.

Precedence, highest first:

  1. A real environment variable. `.env` never overwrites one, so an operator's shell
     always wins over a file on disk, and CI -- which sets no `.env` -- is unaffected.
  2. `.env`.
  3. Nothing; the provider reports itself unavailable and the chain moves on.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
ENV_FILE_ENV = "LEDGERLOOP_ENV_FILE"
PROVIDER_PIN_ENV = "LEDGERLOOP_PROVIDER"


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """Parses the `.env` subset we actually document: KEY=value per line, `#` comments,
    blank lines, optional surrounding quotes, and an optional `export ` prefix.

    A key with an empty value is skipped rather than exported as "". Every provider
    tests availability with `bool(os.environ.get(...))`, so an empty string and an
    absent variable already mean the same thing -- and `.env.example` ships with every
    key present and empty, which must not read as "four providers configured"."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = _strip_quotes(value.strip())
        if not key or not value:
            continue
        values[key] = value
    return values


def load_env_file(path: str | Path | None = None) -> dict[str, str]:
    """Loads `.env` into os.environ without overwriting anything already set.
    Returns the keys it actually applied. Missing or unreadable file is not an error --
    running with no `.env` at all is the documented default.

    Idempotent, so callers may invoke it freely; the second call applies nothing
    because the first call's keys are now real environment variables."""
    if path is None:
        path = os.environ.get(ENV_FILE_ENV) or DEFAULT_ENV_PATH
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}

    applied: dict[str, str] = {}
    for key, value in parse_env_text(text).items():
        # `in os.environ` rather than truthiness: a variable deliberately set to the
        # empty string is a decision ("disable this provider"), and reading it as
        # absent would let .env overwrite it. Note PowerShell removes a variable
        # outright when you assign "" to it, so on that shell the pin below is the
        # reliable way to force a specific provider. See README.
        if key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


def provider_pin() -> str | None:
    """The value of LEDGERLOOP_PROVIDER, lowercased, or None.

    Exists because "unset the key to test the local model" is not portable: in
    PowerShell `$env:GROQ_API_KEY = ""` deletes the variable, so `.env` would supply it
    again on the next run and the chain would quietly go back to the cloud provider you
    were trying to bypass. Pinning states the intent directly instead of arranging the
    absence of a key and hoping every shell agrees on what absence means."""
    pin = os.environ.get(PROVIDER_PIN_ENV, "").strip().lower()
    return pin or None
