"""API tests for GET /forecast/{item_id}, against the real test database.

These exist specifically to catch wiring bugs (e.g. a router built but never registered
with the FastAPI app) that unit tests of the training/prediction logic alone would miss.
"""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(loaded_db):
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(scope="module")
def trained(loaded_db):
    """Run the real training job once against the loaded test database."""
    from app.forecasting.train import train_all
    return train_all(dsn=loaded_db["dsn"])


def any_item_id(loaded_db) -> int:
    with psycopg.connect(loaded_db["dsn"]) as conn:
        row = conn.execute("SELECT item_id FROM forecast_models ORDER BY item_id LIMIT 1").fetchone()
    return row[0]


def test_forecast_returns_points_and_model_info(client, loaded_db, trained):
    item_id = any_item_id(loaded_db)
    body = client.get(f"/forecast/{item_id}").json()
    assert body["item_id"] == item_id
    assert body["horizon_days"] == len(body["forecast"]) > 0
    assert all(p["predicted_units"] >= 0 for p in body["forecast"])
    assert body["model"]["model_type"] == "LinearRegression"
    assert body["model"]["holdout_mae_units_per_day"] >= 0
    assert "cannot anticipate" in body["caveat"]


def test_forecast_unknown_item_is_404(client, loaded_db, trained):
    body = client.get("/forecast/999999")
    assert body.status_code == 404
    assert body.json()["error"]["code"] == "not_found"


def test_forecast_rejects_non_positive_item_id(client, loaded_db, trained):
    assert client.get("/forecast/0").status_code == 422
