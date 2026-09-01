"""`.env` loading and the provider pin.

`.env.example` shipped from day one and the README pointed at it, but nothing read it:
copying it and filling in a key silently did nothing and the run reported
`llm provider: none`. These tests pin the loader's precedence rules and the pin's
fail-closed behaviour. See FAILURES.md (2026-09-01).
"""

from __future__ import annotations

from ledgerloop import env
from ledgerloop.adjudicate import provider as provider_mod


def _write_env(tmp_path, text: str):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_parses_the_documented_subset():
    parsed = env.parse_env_text(
        """
        # a comment

        GROQ_API_KEY=gsk_plain
        GEMINI_API_KEY="gm_double_quoted"
        OPENROUTER_API_KEY='or_single_quoted'
        export OLLAMA_HOST=http://elsewhere:11434
          SPACED_KEY  =  spaced_value
        NO_SEPARATOR_LINE
        """
    )
    assert parsed == {
        "GROQ_API_KEY": "gsk_plain",
        "GEMINI_API_KEY": "gm_double_quoted",
        "OPENROUTER_API_KEY": "or_single_quoted",
        "OLLAMA_HOST": "http://elsewhere:11434",
        "SPACED_KEY": "spaced_value",
    }


def test_empty_values_are_skipped_not_exported_as_blank():
    """.env.example ships every key present and empty. That must read as
    'nothing configured', not as four providers waiting to be called."""
    parsed = env.parse_env_text("GEMINI_API_KEY=\nGROQ_API_KEY=\nOPENROUTER_API_KEY=")
    assert parsed == {}


def test_env_file_populates_a_missing_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    path = _write_env(tmp_path, "GROQ_API_KEY=gsk_from_file")
    applied = env.load_env_file(path)
    assert applied == {"GROQ_API_KEY": "gsk_from_file"}
    assert provider_mod.os.environ["GROQ_API_KEY"] == "gsk_from_file"


def test_real_environment_wins_over_the_file(tmp_path, monkeypatch):
    """An operator's shell always beats a file on disk."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_from_shell")
    path = _write_env(tmp_path, "GROQ_API_KEY=gsk_from_file")
    assert env.load_env_file(path) == {}
    assert provider_mod.os.environ["GROQ_API_KEY"] == "gsk_from_shell"


def test_explicitly_blanked_variable_is_not_repopulated(tmp_path, monkeypatch):
    """A variable set to "" is a decision -- disable this provider -- and .env must
    not overwrite it. (Git Bash passes "" through; PowerShell deletes the variable
    instead, which is why LEDGERLOOP_PROVIDER exists.)"""
    monkeypatch.setenv("GROQ_API_KEY", "")
    path = _write_env(tmp_path, "GROQ_API_KEY=gsk_from_file")
    assert env.load_env_file(path) == {}
    assert provider_mod.os.environ["GROQ_API_KEY"] == ""


def test_missing_env_file_is_not_an_error(tmp_path):
    assert env.load_env_file(tmp_path / "does_not_exist") == {}


def test_loading_twice_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    path = _write_env(tmp_path, "GROQ_API_KEY=gsk_from_file")
    assert env.load_env_file(path) == {"GROQ_API_KEY": "gsk_from_file"}
    assert env.load_env_file(path) == {}  # already a real env var now


# -- provider pin --------------------------------------------------------------------


def _clear_keys(monkeypatch):
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(env.PROVIDER_PIN_ENV, raising=False)
    # never read a developer's real .env during the suite
    monkeypatch.setenv(env.ENV_FILE_ENV, "does-not-exist.env")


def test_pin_selects_the_named_provider_only(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(provider_mod.OllamaProvider, "is_available", lambda self: True)

    assert [p.name for p in provider_mod.resolve_chain()] == ["groq", "ollama", "none"]

    monkeypatch.setenv(env.PROVIDER_PIN_ENV, "ollama")
    assert [p.name for p in provider_mod.resolve_chain()] == ["ollama", "none"]


def test_pin_is_case_insensitive(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setattr(provider_mod.OllamaProvider, "is_available", lambda self: True)
    monkeypatch.setenv(env.PROVIDER_PIN_ENV, "  OLLAMA  ")
    assert [p.name for p in provider_mod.resolve_chain()] == ["ollama", "none"]


def test_pin_to_an_unavailable_provider_fails_closed(monkeypatch):
    """Pinning the local model when no server is running must abstain, never fall
    through to a cloud provider that happens to have a key set."""
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(provider_mod.OllamaProvider, "is_available", lambda self: False)
    monkeypatch.setenv(env.PROVIDER_PIN_ENV, "ollama")
    assert [p.name for p in provider_mod.resolve_chain()] == ["none"]


def test_unrecognised_pin_fails_closed(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv(env.PROVIDER_PIN_ENV, "not-a-provider")
    assert [p.name for p in provider_mod.resolve_chain()] == ["none"]


def test_no_llm_ignores_the_pin_entirely(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setattr(provider_mod.OllamaProvider, "is_available", lambda self: True)
    monkeypatch.setenv(env.PROVIDER_PIN_ENV, "ollama")
    assert [p.name for p in provider_mod.resolve_chain(no_llm=True)] == ["none"]
