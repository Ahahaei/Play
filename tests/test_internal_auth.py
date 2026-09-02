"""FIX.MD A6 — the internal ingest endpoints must not be open.

`/events` and `/webhooks/sp-api` accept a caller-supplied `seller_id` and reach
the auto-execute path. The rest of the suite bypasses the guard through
`dependency_overrides`; this file drops that override and exercises it for real.
"""
import pytest
from fastapi.testclient import TestClient

from app.routers.deps import require_internal_token
from main import app

SECRET = "unit-test-internal-secret"

MONITORING_EVENT = {
    "seller_id": "S001",
    "event_type": "inventory_low",
    "payload": {"sku": "WIDGET-42", "current_quantity": 3},
}
DOMAIN_EVENT = {
    "seller_id": "S001",
    "event_type": "order_created",
    "payload": {"order_id": "ORD-001"},
}


@pytest.fixture
def enforced(bypass_internal_auth, monkeypatch):
    """Drop conftest's override so the real dependency runs."""
    monkeypatch.setenv("INTERNAL_INGEST_SECRET", SECRET)
    app.dependency_overrides.pop(require_internal_token, None)
    yield


@pytest.mark.parametrize("path,body", [
    ("/events", MONITORING_EVENT),
    ("/webhooks/sp-api", DOMAIN_EVENT),
])
def test_missing_token_is_rejected(enforced, path, body):
    with TestClient(app) as client:
        assert client.post(path, json=body).status_code == 401


@pytest.mark.parametrize("path,body", [
    ("/events", MONITORING_EVENT),
    ("/webhooks/sp-api", DOMAIN_EVENT),
])
def test_wrong_token_is_rejected(enforced, path, body):
    with TestClient(app) as client:
        resp = client.post(path, json=body, headers={"X-Internal-Token": "guess"})
    assert resp.status_code == 401


@pytest.mark.parametrize("path,body", [
    ("/events", MONITORING_EVENT),
    ("/webhooks/sp-api", DOMAIN_EVENT),
])
def test_correct_token_is_accepted(enforced, path, body):
    with TestClient(app) as client:
        resp = client.post(path, json=body, headers={"X-Internal-Token": SECRET})
    assert resp.status_code == 202


def test_unset_secret_fails_closed(enforced, monkeypatch):
    # An unconfigured deployment rejects everything rather than admitting it.
    monkeypatch.delenv("INTERNAL_INGEST_SECRET")
    with TestClient(app) as client:
        resp = client.post("/events", json=MONITORING_EVENT, headers={"X-Internal-Token": ""})
    assert resp.status_code == 401


def test_reading_an_event_stays_open(enforced):
    # Only the write paths are guarded; GET /events/{id} is unchanged.
    with TestClient(app) as client:
        assert client.get("/events/does-not-exist").status_code == 404
