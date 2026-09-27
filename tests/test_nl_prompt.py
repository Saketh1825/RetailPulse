"""Tests for app/nl_sql/prompt.py: the prompt must expose only the whitelisted schema."""
from __future__ import annotations

from app.nl_sql.prompt import build_messages


def test_mentions_only_whitelisted_tables():
    messages = build_messages("test question")
    system = messages[0]["content"]
    for table in ("items", "sales", "forecasts"):
        assert table in system

    # The read-only role cannot reach these; the model should never be told they exist.
    for forbidden in ("etl_runs", "forecast_models", "nl_query_log"):
        assert forbidden not in system.lower()


def test_states_the_hard_rules():
    system = build_messages("test question")[0]["content"]
    for phrase in ("exactly one", "SELECT", "LIMIT"):
        assert phrase in system
    for forbidden_stmt in ("INSERT", "UPDATE", "DELETE", "DROP"):
        assert forbidden_stmt in system  # named as disallowed, not usable


def test_ends_with_the_users_question():
    messages = build_messages("How many bakery items are there?")
    assert messages[-1] == {"role": "user", "content": "How many bakery items are there?"}
    assert messages[0]["role"] == "system"


def test_includes_few_shot_examples_as_alternating_turns():
    messages = build_messages("q")
    roles = [m["role"] for m in messages]
    # system, then (user, assistant) pairs for each example, then the final user question
    assert roles[0] == "system"
    assert roles[1:-1:2] == ["user"] * (len(roles[1:-1]) // 2)
    assert roles[2:-1:2] == ["assistant"] * (len(roles[1:-1]) // 2)
