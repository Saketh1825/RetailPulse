"""Tests for app/config.py -- including a regression test for a real bug found while preparing
this project: python-dotenv does NOT strip a trailing `# comment` on the same line as a KEY=value
pair, so `API_KEY=   # if set, ...` was silently parsed as API_KEY having the *comment text* as its
value, not an empty/unset key. That accidentally "locked" POST /query behind an unguessable key.
The fix is structural (comments go on their own line), which is exactly what this test guards.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings, get_settings

ENV_FILES = [PROJECT_ROOT / ".env.example"]
if (PROJECT_ROOT / ".env").exists():
    ENV_FILES.append(PROJECT_ROOT / ".env")

# KEY=value followed by whitespace then a '#' on the SAME line, with no quotes around the value
# (a quoted value legitimately containing '#' would be fine; dotenv only mishandles the bare case).
_INLINE_COMMENT_RE = re.compile(r'^[A-Z_][A-Z0-9_]*=(?!["\']).*\s#')


@pytest.mark.parametrize("path", ENV_FILES, ids=lambda p: p.name)
def test_env_file_inline_comments_are_not_swallowed_as_values(path: Path):
    offending = [line for line in path.read_text().splitlines() if _INLINE_COMMENT_RE.match(line)]
    assert offending == [], (
        f"{path.name} has an inline '# comment' on the same line as a KEY=value pair. "
        f"python-dotenv treats the comment text itself as the value -- move it to its own line. "
        f"Offending line(s): {offending}"
    )


def test_empty_string_secret_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    s = get_settings()
    assert s.api_key is None
    assert s.llm_api_key is None
    get_settings.cache_clear()


def test_whitespace_only_secret_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("API_KEY", "   ")
    get_settings.cache_clear()
    assert get_settings().api_key is None
    get_settings.cache_clear()


def test_a_real_value_is_kept_as_a_secret(monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret-value")
    get_settings.cache_clear()
    s = get_settings()
    assert s.api_key is not None
    assert s.api_key.get_secret_value() == "s3cret-value"
    monkeypatch.delenv("API_KEY", raising=False)
    get_settings.cache_clear()


def test_reproduces_the_original_bug_shape_for_documentation():
    """Demonstrates -- without touching real env vars -- exactly what went wrong: an inline comment
    is just a normal non-empty string to Settings, so nothing downstream can tell it apart from a
    real key. This is why the fix has to be "never write it that way" (tested above), not a smarter
    validator."""
    s = Settings(database_url="postgresql://x/y", readonly_database_url="postgresql://x/y",
                 api_key="# if set, POST /query requires header  X-API-Key: <value>")
    assert s.api_key is not None
    assert s.api_key.get_secret_value().startswith("#")  # accepted as a "real" key -- the bug, reproduced
