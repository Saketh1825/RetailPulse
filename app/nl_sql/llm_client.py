"""Call an LLM to translate a natural-language question into SQL.

Exactly one LLM call per question (see docs/ARCHITECTURE.md: no agent loop, no retries that
change the question, no chained calls). Works with any OpenAI-compatible chat-completions
endpoint (Groq, Gemini's OpenAI-compat endpoint, etc.) via plain HTTP -- no vendor SDK.

IMPORTANT (read before treating this as "done"): the sandbox this project was built in only allows
outbound network access to a short list of package-registry domains (see docs/NL_SQL_SECURITY.md).
It cannot reach api.groq.com or any other LLM endpoint. LLMClient below has therefore been tested
with a mocked HTTP transport (tests/test_nl_llm_client.py exercises the real request/response
handling code with no live network call) but NOT against a real LLM API. Before relying on this in
a demo, run it yourself, from an environment with real internet access, against a real key.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import httpx

_FENCE_RE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


class LLMError(Exception):
    """Raised for any failure talking to the LLM: network, timeout, bad status, bad shape."""


@dataclass
class LLMResult:
    sql: str
    elapsed_ms: int


def _strip_fences(text: str) -> str:
    """Defensively strip markdown code fences some models wrap output in even when told not to.
    This is a convenience, not a safety measure -- the validator does not trust this output."""
    return _FENCE_RE.sub("", text).strip()


class LLMClient:
    """Real client: POSTs to {base_url}/chat/completions in OpenAI chat-completions shape.

    Accepts an optional pre-built httpx.Client so tests can inject a MockTransport instead of
    making a real network call; production code just uses the default (a real client)."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout_seconds: float = 20.0, max_output_tokens: int = 400,
                 client: httpx.Client | None = None):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._max_tokens = max_output_tokens
        self._client = client or httpx.Client()

    def generate_sql(self, messages: list[dict[str, str]]) -> LLMResult:
        t0 = time.perf_counter()
        try:
            resp = self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={"model": self._model, "messages": messages, "max_tokens": self._max_tokens,
                      "temperature": 0},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMError("the AI service timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMError("the AI service could not be reached") from exc

        if resp.status_code != 200:
            raise LLMError(f"the AI service returned an error (status {resp.status_code})")

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("the AI service returned an unexpected response shape") from exc

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return LLMResult(sql=_strip_fences(content), elapsed_ms=elapsed_ms)


class FakeLLMClient:
    """Deterministic stand-in for tests and offline demos: returns a fixed SQL string (or raises
    LLMError) regardless of the question, so tests can exercise the validator/endpoint pipeline
    without any network access. Also used to simulate a misbehaving/compromised LLM: the pipeline
    must reject bad output from this fake exactly as it would from a real model."""

    def __init__(self, fixed_sql: str | None = None, raise_error: str | None = None):
        self._fixed_sql = fixed_sql or "SELECT * FROM items"
        self._raise_error = raise_error

    def generate_sql(self, messages: list[dict[str, str]]) -> LLMResult:
        if self._raise_error:
            raise LLMError(self._raise_error)
        return LLMResult(sql=self._fixed_sql, elapsed_ms=1)
