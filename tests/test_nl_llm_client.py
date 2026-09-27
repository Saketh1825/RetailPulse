"""Tests for app/nl_sql/llm_client.py's LLMClient, using httpx.MockTransport so the real HTTP
request/response handling code is exercised without any live network call.

This sandbox cannot reach a real LLM API (see the module docstring in llm_client.py), so these
tests are the strongest verification available here. A live call should still be run once, by a
human, against a real key, before treating the NL-to-SQL feature as demo-ready end to end.
"""
from __future__ import annotations

import httpx
import pytest

from app.nl_sql.llm_client import LLMClient, LLMError


def _client_with(handler) -> LLMClient:
    transport = httpx.MockTransport(handler)
    return LLMClient(base_url="https://fake-llm.example/v1", api_key="test-key",
                      model="test-model", client=httpx.Client(transport=transport))


def test_sends_correct_request_shape_and_parses_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = httpx.Request.read(request) and request.read()
        return httpx.Response(200, json={"choices": [{"message": {"content": "SELECT 1"}}]})

    client = _client_with(handler)
    result = client.generate_sql([{"role": "user", "content": "q"}])

    assert result.sql == "SELECT 1"
    assert result.elapsed_ms >= 0
    assert seen["url"] == "https://fake-llm.example/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert b'"test-model"' in seen["body"]


@pytest.mark.parametrize("wrapped,expected", [
    ("```sql\nSELECT 1\n```", "SELECT 1"),
    ("```\nSELECT 1\n```", "SELECT 1"),
    ("SELECT 1", "SELECT 1"),
    ("  SELECT 1  ", "SELECT 1"),
])
def test_strips_markdown_fences(wrapped, expected):
    client = _client_with(lambda r: httpx.Response(
        200, json={"choices": [{"message": {"content": wrapped}}]}))
    assert client.generate_sql([]).sql == expected


def test_non_200_status_raises_llm_error():
    client = _client_with(lambda r: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(LLMError):
        client.generate_sql([])


def test_timeout_raises_llm_error():
    def handler(request: httpx.Request):
        raise httpx.TimeoutException("timed out", request=request)
    client = _client_with(handler)
    with pytest.raises(LLMError):
        client.generate_sql([])


def test_connect_error_raises_llm_error():
    def handler(request: httpx.Request):
        raise httpx.ConnectError("refused", request=request)
    client = _client_with(handler)
    with pytest.raises(LLMError):
        client.generate_sql([])


@pytest.mark.parametrize("bad_json", [
    {},
    {"choices": []},
    {"choices": [{}]},
    {"choices": [{"message": {}}]},
])
def test_malformed_response_shape_raises_llm_error(bad_json):
    client = _client_with(lambda r: httpx.Response(200, json=bad_json))
    with pytest.raises(LLMError):
        client.generate_sql([])


def test_not_json_response_raises_llm_error():
    client = _client_with(lambda r: httpx.Response(200, text="not json"))
    with pytest.raises(LLMError):
        client.generate_sql([])
