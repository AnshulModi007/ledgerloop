"""LLM provider abstraction + fallback chain. First available wins:

  1. GEMINI_API_KEY set      -> Google AI Studio free tier
  2. GROQ_API_KEY set        -> Groq free tier
  3. OPENROUTER_API_KEY set  -> OpenRouter free-tier model
  4. Local Ollama reachable on localhost:11434 -> local model
  5. None available          -> NullProvider, which abstains on every call

NullProvider is what makes `make demo` work with zero API keys: the pipeline
completes, everything tier3 would have adjudicated flows to the exception queue
instead, and the report says so honestly. This is a documented operating mode, not
a failure -- see IMPLEMENTATION.md sections 4 and 10, and the `--no-llm` CLI flag.

Every provider here uses plain stdlib HTTP (no vendor SDK) against each service's
publicly documented free-tier REST endpoint, so there's no extra dependency to
install just to reach the LLM tier. None of these have been exercised against a
live key in this environment -- see FAILURES.md.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

REQUEST_TIMEOUT_SECONDS = 20
OLLAMA_PROBE_TIMEOUT_SECONDS = 0.5
OLLAMA_HOST_ENV = "OLLAMA_HOST"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"

_TRANSPORT_ERRORS = (urllib.error.URLError, OSError, ValueError)


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def is_available(self) -> bool:
        """Cheap, local check only -- an env var being set, or a quick reachability
        probe. Never a real inference call."""

    @abstractmethod
    def complete(self, prompt: str) -> str | None:
        """Returns the raw text response, or None on any failure (network error,
        timeout, rate limit, non-2xx status, unparseable body). Never raises."""


_DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    # Some providers front their API with bot-fingerprint filtering (observed:
    # Cloudflare error 1010) that rejects urllib's default User-Agent outright,
    # before the request ever reaches the provider's own API logic. A plain
    # browser-shaped UA clears it. See FAILURES.md.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ledgerloop/0.1",
}


def _post_json(url: str, payload: dict, headers: dict) -> dict | None:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={**_DEFAULT_HEADERS, **headers})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except _TRANSPORT_ERRORS:
        return None


class GeminiProvider(LLMProvider):
    name = "gemini"
    # Not live-verified against a real key (none available in this environment) --
    # gemini-1.5-flash, the obvious choice from an earlier model generation, is
    # retired. gemini-2.5-flash was still a documented free-tier model as of
    # 2026-08-23; if this errors, check https://ai.google.dev/gemini-api/docs/pricing
    # and update. See FAILURES.md.
    model = "gemini-2.5-flash"

    def is_available(self) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY"))

    def complete(self, prompt: str) -> str | None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return None
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:"
            f"generateContent?key={api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        response = _post_json(url, payload, headers={})
        if response is None:
            return None
        try:
            return response["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return None


class GroqProvider(LLMProvider):
    name = "groq"
    # Verified live against the free tier on 2026-08-23 -- Groq's catalog turns over
    # fast, so if this 404s ("model_not_found"), check GET /openai/v1/models with
    # your key and update this. See FAILURES.md.
    model = "openai/gpt-oss-20b"

    def is_available(self) -> bool:
        return bool(os.environ.get("GROQ_API_KEY"))

    def complete(self, prompt: str) -> str | None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            return None
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        response = _post_json(
            "https://api.groq.com/openai/v1/chat/completions",
            payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if response is None:
            return None
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None


class OpenRouterProvider(LLMProvider):
    name = "openrouter"
    # Not live-verified against a real key (none available in this environment) --
    # OpenRouter's free-model lineup turns over often (models get retired with
    # little notice). meta-llama/llama-3.3-70b-instruct:free was live as of
    # 2026-08-23; if this 404s, check https://openrouter.ai/models?max_price=0 and
    # update. See FAILURES.md.
    model = "meta-llama/llama-3.3-70b-instruct:free"

    def is_available(self) -> bool:
        return bool(os.environ.get("OPENROUTER_API_KEY"))

    def complete(self, prompt: str) -> str | None:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return None
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        response = _post_json(
            "https://openrouter.ai/api/v1/chat/completions",
            payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if response is None:
            return None
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None


class OllamaProvider(LLMProvider):
    name = "ollama"
    model = "llama3.1"

    @property
    def _host(self) -> str:
        return os.environ.get(OLLAMA_HOST_ENV, DEFAULT_OLLAMA_HOST)

    def is_available(self) -> bool:
        try:
            urllib.request.urlopen(f"{self._host}/api/tags", timeout=OLLAMA_PROBE_TIMEOUT_SECONDS)
            return True
        except _TRANSPORT_ERRORS:
            return False

    def complete(self, prompt: str) -> str | None:
        payload = {"model": self.model, "prompt": prompt, "stream": False, "format": "json"}
        response = _post_json(f"{self._host}/api/generate", payload, headers={})
        if response is None:
            return None
        return response.get("response")


class NullProvider(LLMProvider):
    """Abstains on every call. See module docstring."""

    name = "none"

    def is_available(self) -> bool:
        return True

    def complete(self, prompt: str) -> str | None:
        return None


def resolve_chain(*, no_llm: bool = False) -> list[LLMProvider]:
    """First-available-wins resolution order. NullProvider always terminates the
    chain, so callers can loop through it unconditionally without a special case."""
    if no_llm:
        return [NullProvider()]
    candidates: list[LLMProvider] = [
        GeminiProvider(),
        GroqProvider(),
        OpenRouterProvider(),
        OllamaProvider(),
    ]
    available = [p for p in candidates if p.is_available()]
    return [*available, NullProvider()]


def complete_with_fallback(chain: list[LLMProvider], prompt: str) -> tuple[str | None, str, int]:
    """Tries each provider in order; the first non-None response wins. On rate-limit
    exhaustion or any transport failure mid-chain, falls through to the next provider
    rather than failing the call. Returns (response_text, provider_name, real_attempts).

    provider_name is "none" specifically when a NullProvider was reached (meaning no
    LLM is configured at all, so retrying won't help) -- resolve_chain() always
    appends one, so this is the normal way a fully-exhausted production chain ends.
    A chain with no NullProvider at all (e.g. in a test) that exhausts every real
    provider instead returns the last provider's own name, so a caller can tell "a
    real provider failed this round" apart from "no LLM is configured" and retry
    accordingly. real_attempts counts only actual network calls made (excludes
    NullProvider), for free-tier cost/rate-limit accounting.
    """
    attempts = 0
    last_provider_name = "none"
    for provider in chain:
        if isinstance(provider, NullProvider):
            return None, provider.name, attempts
        attempts += 1
        last_provider_name = provider.name
        text = provider.complete(prompt)
        if text is not None:
            return text, provider.name, attempts
    return None, last_provider_name, attempts
