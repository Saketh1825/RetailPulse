"""Forecasting tests.

Unit tests use tiny hand-checkable fixtures (no database). Integration tests train on the real loaded
test database. The most important test here is `test_holdout_is_never_seen_before_scoring`: it proves
the chronological split has no leakage by changing the holdout values and asserting nothing moves.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg
import pytest

from app.forecasting.data import Series, build_series, is_active
from app.forecasting.features import FEATURE_NAMES, MIN_HISTORY, build_training_matrix, feature_row
from app.forecasting.metrics import mae, wape
from app.forecasting.predict import recursive_forecast
from app.forecasting.train import N_ORIGINS, evaluate_at_cutoff, train_all, train_item

D0 = date(2023, 1, 2)          # a Monday


def weekly_series(n: int, pattern=(10, 12, 11, 13, 20, 30, 25), noise=0.0, seed=0) -> Series:
    rng = np.random.default_rng(seed)
    vals = [float(pattern[i % 7]) + (rng.normal(0, noise) if noise else 0.0) for i in range(n)]
    return Series([D0 + timedelta(days=i) for i in range(n)], vals, [True] * n)


# ----------------------------------------------------------------------------------- metrics
def test_mae_by_hand():
    # errors: 1, 2, 0, 3  -> mean 1.5
    assert mae([10, 20, 30, 40], [11, 18, 30, 37]) == pytest.approx(1.5)


def test_wape_by_hand():
    # sum|err| = 6, sum(actual) = 100 -> 0.06
    assert wape([10, 20, 30, 40], [11, 18, 30, 37]) == pytest.approx(0.06)


def test_metrics_ignore_imputed_days():
    actual, pred, mask = [10, 999, 30], [12, 0, 30], [True, False, True]
    assert mae(actual, pred, mask) == pytest.approx(1.0)          # the 999-vs-0 day is not scored
    assert wape(actual, pred, mask) == pytest.approx(2 / 40)


def test_metrics_reject_bad_input():
    with pytest.raises(ValueError):
        mae([1, 2], [1])
    with pytest.raises(ValueError):
        mae([1, 2], [1, 2], [False, False])                        # nothing observed -> refuse, don't return NaN


def test_wape_defined_with_zero_days_unlike_mape():
    assert wape([0, 0, 10], [1, 1, 8]) == pytest.approx(4 / 10)
    assert math.isnan(wape([0, 0], [1, 1]))                        # all-zero actuals: undefined, and says so


# ----------------------------------------------------------------------------------- features
def test_feature_row_has_one_value_per_feature_name():
    s = weekly_series(40)
    assert len(feature_row(s.values, s.dates[30], 30, s.dates[0])) == len(FEATURE_NAMES)


def test_feature_row_needs_history():
    s = weekly_series(40)
    with pytest.raises(ValueError):
        feature_row(s.values, s.dates[MIN_HISTORY - 1], MIN_HISTORY - 1, s.dates[0])


def test_features_never_use_the_target_day_or_later():
    """Corrupt every value at index >= i: the features for day i must be identical."""
    s = weekly_series(60)
    i = 40
    a = feature_row(s.values, s.dates[i], i, s.dates[0])
    corrupted = s.values[:i] + [1e9] * (len(s.values) - i)
    b = feature_row(corrupted, s.dates[i], i, s.dates[0])
    assert a == b


def test_feature_values_match_hand_computation():
    vals = [float(v) for v in range(1, 41)]                        # 1..40
    dates = [D0 + timedelta(days=k) for k in range(40)]
    i = 30
    f = dict(zip(FEATURE_NAMES, feature_row(vals, dates[i], i, dates[0]), strict=True))
    assert f["lag_7"] == vals[23]
    assert f["lag_14"] == vals[16]
    assert f["roll_mean_7_at_lag_7"] == pytest.approx(np.mean(vals[17:24]))    # days i-13 .. i-7
    assert f["trend_years"] == pytest.approx(30 / 365.25)
    weekday = dates[i].weekday()
    for k, name in enumerate(["dow_tue", "dow_wed", "dow_thu", "dow_fri", "dow_sat", "dow_sun"], start=1):
        assert f[name] == (1.0 if weekday == k else 0.0)


def test_training_matrix_skips_warmup_and_imputed_days():
    s = weekly_series(40)
    observed = [True] * 40
    observed[20] = False                                           # one imputed day
    X, y = build_training_matrix(s.dates, s.values, observed, s.dates[0])
    assert X.shape == (40 - MIN_HISTORY - 1, len(FEATURE_NAMES))
    assert len(y) == len(X)
    assert not np.isnan(X).any()


# ----------------------------------------------------------------------------------- series building
def test_gap_is_imputed_but_flagged_and_explicit_zero_is_kept_as_real():
    rows = [(date(2023, 1, 1), 10), (date(2023, 1, 2), 0), (date(2023, 1, 4), 30)]   # Jan 3 missing, Jan 2 = real zero
    s = build_series(rows, date(2023, 1, 4))
    assert s.observed == [True, True, False, True]
    assert s.values[1] == 0.0                                      # real zero stays zero
    assert s.values[2] == pytest.approx(15.0)                      # gap = linear interpolation between 0 and 30


def test_is_active_flags_discontinued_items():
    end = date(2023, 12, 31)
    dates = [date(2023, 1, 1) + timedelta(days=i) for i in range(200)]
    live = Series(dates, [5.0] * 200, [True] * 200)
    assert not is_active(live, end, 28)                            # last real sale is Jul 2023 -> discontinued
    dates2 = [end - timedelta(days=i) for i in reversed(range(200))]
    assert is_active(Series(dates2, [5.0] * 200, [True] * 200), end, 28)


# ----------------------------------------------------------------------------------- prediction
class _AddOneToLag7:
    """Stub model: prediction = lag_7 + 1. Lets us verify the recursion feeds predictions back in."""

    def predict(self, X):
        return [row[0] + 1.0 for row in X]


def test_recursive_forecast_feeds_predictions_back_as_lags():
    s = weekly_series(30)
    out = recursive_forecast(_AddOneToLag7(), s.dates, s.values, 14, s.dates[0])
    assert [d for d, _ in out] == [s.dates[-1] + timedelta(days=k) for k in range(1, 15)]
    # day 1 uses a real lag; day 8 uses the PREDICTION made for day 1 (+1 again)
    assert out[0][1] == pytest.approx(s.values[-7] + 1)
    assert out[7][1] == pytest.approx(s.values[-7] + 2)


def test_recursive_forecast_clips_negative_predictions_to_zero():
    class Negative:
        def predict(self, X):
            return [-5.0 for _ in X]

    s = weekly_series(30)
    assert all(y == 0.0 for _, y in recursive_forecast(Negative(), s.dates, s.values, 7, s.dates[0]))


# ----------------------------------------------------------------------------------- evaluation
def test_model_recovers_a_clean_weekly_pattern():
    """Sanity check: on a perfectly periodic series the model must be nearly exact."""
    s = weekly_series(300)
    r = evaluate_at_cutoff(s, 300 - 28, 28)
    assert r.mae < 0.5
    assert r.mae < r.mean_mae                                      # and clearly beats 'always the average'


def test_holdout_is_never_seen_before_scoring():
    """NO LEAKAGE: replace the held-out days with garbage; the predictions must not change at all."""
    s = weekly_series(300, noise=2.0, seed=1)
    cutoff, horizon = 300 - 28, 28
    base = evaluate_at_cutoff(s, cutoff, horizon)
    tampered_vals = s.values[:cutoff] + [12345.0] * (300 - cutoff)
    tampered = Series(s.dates, tampered_vals, s.observed)
    after = evaluate_at_cutoff(tampered, cutoff, horizon)
    assert after.predicted == base.predicted                       # identical predictions
    assert after.actual != base.actual                             # while the truth changed (so the test has teeth)
    assert after.mae != base.mae


def test_rolling_origins_are_chronological_and_non_overlapping():
    s = weekly_series(400, noise=1.0, seed=2)
    res = train_item(1, "x", s, s.dates[-1], 28, 120)
    assert res.status == "trained" and len(res.origins) == N_ORIGINS
    starts = [o.cutoff for o in res.origins]                       # most recent first
    assert starts == sorted(starts, reverse=True)
    assert starts[0] == s.dates[-28]                               # primary window = the LAST 28 days
    assert all((a - b).days == 28 for a, b in zip(starts, starts[1:], strict=False))


def test_short_and_inactive_series_are_skipped_not_crashed():
    short = weekly_series(60)
    assert train_item(1, "short", short, short.dates[-1], 28, 120).status == "skipped_short_history"
    old = weekly_series(300)
    assert train_item(2, "old", old, old.dates[-1] + timedelta(days=100), 28, 120).status == "skipped_inactive"


def test_zero_sales_and_flat_series_do_not_crash():
    n = 200
    flat = Series([D0 + timedelta(days=i) for i in range(n)], [0.0] * n, [True] * n)
    res = train_item(3, "dead-but-listed", flat, flat.dates[-1], 28, 120)
    assert res.status == "trained"
    assert all(y >= 0 for _, y in res.forecast)


# ----------------------------------------------------------------------------------- integration
pytestmark_integration = pytest.mark.integration


@pytest.fixture(scope="module")
def trained(loaded_db, tmp_path_factory):
    model_dir = tmp_path_factory.mktemp("models")
    metrics = tmp_path_factory.mktemp("metrics") / "metrics.json"
    results = train_all(dsn=loaded_db["dsn"], horizon=28, model_dir=model_dir, metrics_path=metrics)
    return {"results": results, "model_dir": model_dir, "metrics": metrics, "dsn": loaded_db["dsn"]}


@pytest.mark.integration
def test_every_active_item_is_trained_and_forecast_stored(trained):
    ok = [r for r in trained["results"] if r.status == "trained"]
    assert len(ok) >= 20
    with psycopg.connect(trained["dsn"]) as c:
        n_models = c.execute("SELECT count(*) FROM forecast_models").fetchone()[0]
        per_item = c.execute("SELECT item_id, count(*), min(forecast_date), max(forecast_date) FROM forecasts "
                             "GROUP BY item_id").fetchall()
        last_sale = c.execute("SELECT max(sale_date) FROM sales").fetchone()[0]
    assert n_models == len(ok) == len(per_item)
    for _, n, first, last in per_item:
        assert n == 28
        assert first == last_sale + timedelta(days=1)              # forecast starts the day after the data ends
        assert last == last_sale + timedelta(days=28)


@pytest.mark.integration
def test_stored_forecasts_are_valid_numbers(trained):
    with psycopg.connect(trained["dsn"]) as c:
        bad = c.execute("SELECT count(*) FROM forecasts WHERE predicted_units < 0 "
                        "OR predicted_units = 'NaN'::float OR predicted_units = 'Infinity'::float").fetchone()[0]
    assert bad == 0


@pytest.mark.integration
def test_model_beats_trivial_baselines_on_average(trained):
    """The honest justification for using a model at all."""
    ok = [r for r in trained["results"] if r.status == "trained"]
    macro = lambda f: float(np.mean([f(r.primary) for r in ok]))   # noqa: E731
    assert macro(lambda c: c.mae) < macro(lambda c: c.naive_mae)
    assert macro(lambda c: c.mae) < macro(lambda c: c.mean_mae)


@pytest.mark.integration
def test_test_window_is_after_training_window_in_database(trained):
    with psycopg.connect(trained["dsn"]) as c:
        rows = c.execute("SELECT train_end, test_start, test_end FROM forecast_models").fetchall()
    assert rows
    for train_end, test_start, test_end in rows:
        assert train_end < test_start <= test_end


@pytest.mark.integration
def test_model_files_saved_and_reload_to_same_forecast(trained):
    import joblib

    res = next(r for r in trained["results"] if r.status == "trained")
    bundle = joblib.load(trained["model_dir"] / f"item_{res.item_id}.joblib")
    assert bundle["features"] == FEATURE_NAMES
    with psycopg.connect(trained["dsn"]) as c:
        rows = c.execute("SELECT sale_date, units_sold FROM sales WHERE item_id=%s ORDER BY sale_date",
                         (res.item_id,)).fetchall()
    s = build_series(rows, rows[-1][0])
    again = recursive_forecast(bundle["model"], s.dates, s.values, 28, bundle["t0"])
    assert [round(y, 6) for _, y in again] == [round(y, 6) for _, y in res.forecast]


@pytest.mark.integration
def test_training_is_repeatable(loaded_db, trained, tmp_path):
    """Same data -> same forecasts (LinearRegression is deterministic; no hidden randomness)."""
    with psycopg.connect(loaded_db["dsn"]) as c:
        before = c.execute("SELECT item_id, forecast_date, predicted_units FROM forecasts ORDER BY 1, 2").fetchall()
    train_all(dsn=loaded_db["dsn"], horizon=28, model_dir=tmp_path)
    with psycopg.connect(loaded_db["dsn"]) as c:
        after = c.execute("SELECT item_id, forecast_date, predicted_units FROM forecasts ORDER BY 1, 2").fetchall()
    assert before == after


@pytest.mark.integration
def test_metrics_json_is_complete_and_consistent(trained):
    import json

    m = json.loads(trained["metrics"].read_text())
    assert m["items_trained"] == len([r for r in trained["results"] if r.status == "trained"])
    assert m["primary_window"]["macro_mae_model"] < m["primary_window"]["macro_mae_seasonal_naive"]
    assert 0 < m["primary_window"]["overall_wape_model"] < 1
    assert len(m["per_item"]) == m["items_trained"]
    assert set(m["per_item"][0]) >= {"mae", "wape", "naive_mae", "mean_mae", "actual", "predicted", "coefficients"}


@pytest.mark.integration
def test_forecast_table_rejects_negative_predictions(trained):
    with psycopg.connect(trained["dsn"]) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("INSERT INTO forecasts (item_id, forecast_date, predicted_units) "
                  "SELECT id, DATE '2099-01-01', -1 FROM items LIMIT 1")


def test_pandas_is_only_used_for_series_construction():
    """Documentation-by-test: build_series returns plain lists, so no pandas objects leak into feature code."""
    s = build_series([(date(2023, 1, 1), 1), (date(2023, 1, 3), 3)], date(2023, 1, 3))
    assert isinstance(s.values, list) and isinstance(s.dates[0], date) and not isinstance(s.dates[0], pd.Timestamp)
