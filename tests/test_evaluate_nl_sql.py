"""Sanity-checks for scripts/evaluate_nl_sql.py -- the harness's *comparison logic*, not an LLM's
correctness. No network call, no LLM_API_KEY needed: a fake generator that returns each question's
own reference_sql stands in for the LLM. If the harness is correct, that must score 100%, because
comparing a query against itself can only ever match. This proves the harness would report a real
score honestly if it were run against a real model -- it does not simulate what that score would be.
"""
from __future__ import annotations

import pytest

from app.nl_sql.llm_client import LLMResult
from scripts.evaluate_nl_sql import evaluate, load_questions

pytestmark = pytest.mark.integration


class _ReferenceEchoGenerator:
    """Stands in for an LLM that always gets it right, by literally returning the reference SQL.
    Used only to test the harness's own scoring logic in isolation from any real model."""

    def generate_sql(self, messages: list[dict[str, str]]) -> LLMResult:
        question = messages[-1]["content"]
        for q in load_questions():
            if q["question"] == question:
                return LLMResult(sql=q["reference_sql"], elapsed_ms=0)
        raise AssertionError(f"no fixture question matches: {question!r}")


def test_questions_file_is_well_formed():
    questions = load_questions()
    assert len(questions) >= 15
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), "question ids must be unique"
    for q in questions:
        assert q["question"].strip()
        assert q["reference_sql"].strip()


def test_harness_scores_100_percent_when_generator_echoes_the_reference_sql(loaded_db):
    from tests.conftest import TEST_RO_URL
    report = evaluate(_ReferenceEchoGenerator(), loaded_db["dsn"], TEST_RO_URL, max_rows=500,
                       questions=load_questions())
    assert report["summary"]["correctness_rate_pct"] == 100.0
    assert report["summary"]["by_outcome"]["pass"] == report["summary"]["total"]
    assert all(r["outcome"] == "pass" for r in report["results"])


def test_harness_reports_mismatch_for_a_wrong_query(loaded_db):
    from tests.conftest import TEST_RO_URL
    questions = load_questions()[:1]
    questions[0] = {**questions[0], "reference_sql": "SELECT COUNT(*) AS n FROM items"}  # all items

    class _WrongGenerator:
        def generate_sql(self, messages):
            # deliberately answers a narrower question than the one actually asked
            return LLMResult(sql="SELECT COUNT(*) AS n FROM items WHERE category = 'Dairy'", elapsed_ms=0)

    report = evaluate(_WrongGenerator(), loaded_db["dsn"], TEST_RO_URL, max_rows=500, questions=questions)
    assert report["results"][0]["outcome"] == "mismatch"
    assert report["summary"]["correctness_rate_pct"] == 0.0
