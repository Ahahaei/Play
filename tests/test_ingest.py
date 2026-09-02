"""Phase A / A1, A3, A5, A6 — the ingest boundary.

`POST /webhooks/{platform}`: verify → normalize → resolve → one transaction →
202. The invariant that matters most: a redelivery stores one event and
enqueues one job, not two.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import store
from app.db.engine import SessionLocal
from app.db.models import EventRow, JobRow, SellerPlatformAccountRow
from app.models.event import EventType
from app.models.job import JobStatus
from app.models.platform import Platform, ShopeeCredentials
from app.platforms.shopee.adapter import push_signature
from main import app

PARTNER_KEY = "ingest-test-partner-key"
SHOP_ID = 30011


@pytest.fixture(autouse=True)
def platform_secrets(monkeypatch):
    monkeypatch.setenv("INTERNAL_INGEST_SECRET", "test-internal-secret")
    monkeypatch.setenv("SHOPEE_PARTNER_KEY", PARTNER_KEY)


@pytest.fixture(autouse=True)
def shopee_account():
    """S001 also sells on Shopee, so shop_id 30011 resolves."""
    store.upsert_account(
        platform=Platform.SHOPEE,
        external_id=str(SHOP_ID),
        seller_id="S001",
        credentials=ShopeeCredentials(partner_id="1000000", partner_key=PARTNER_KEY),
    )
    yield
    db = SessionLocal()
    try:
        db.execute(
            delete(SellerPlatformAccountRow)
            .where(SellerPlatformAccountRow.platform == Platform.SHOPEE.value)
        )
        db.commit()
    finally:
        db.close()


def amazon_body(notification_id="nid-001", status="Unshipped", seller="A_MOCK_S001"):
    return json.dumps({
        "NotificationType": "ORDER_CHANGE",
        "EventTime": "2026-09-02T01:35:06.382Z",
        "Payload": {"OrderChangeNotification": {
            "SellerId": seller,
            "AmazonOrderId": "123-4567891-4567891",
            "Summary": {
                "MarketplaceId": "A2Q3Y263D00KWC",
                "OrderStatus": status,
                "OrderItems": [{"SellerSKU": "WIDGET-42", "Quantity": 3}],
            },
        }},
        "NotificationMetadata": {"NotificationId": notification_id},
    }).encode()


def shopee_body(ordersn="2112132KAD867D", status="READY_TO_SHIP", update_time=1639390316):
    return json.dumps({
        "data": {"items": [], "ordersn": ordersn, "status": status, "update_time": update_time},
        "shop_id": SHOP_ID,
        "code": 3,
        "timestamp": update_time,
    }).encode()


def post_amazon(client, body=None, token="test-internal-secret"):
    return client.post(
        "/webhooks/amazon",
        content=body if body is not None else amazon_body(),
        headers={"X-Internal-Token": token, "Content-Type": "application/json"},
    )


def post_shopee(client, body=None, key=PARTNER_KEY):
    body = body if body is not None else shopee_body()
    url = "http://testserver/webhooks/shopee"
    return client.post(
        "/webhooks/shopee",
        content=body,
        headers={"Authorization": push_signature(url, body, key), "Content-Type": "application/json"},
    )


def stored_events():
    db = SessionLocal()
    try:
        return db.execute(select(EventRow)).scalars().all()
    finally:
        db.close()


def stored_jobs():
    db = SessionLocal()
    try:
        return db.execute(select(JobRow)).scalars().all()
    finally:
        db.close()


# --- routing and rejection ---

def test_unknown_platform_is_404():
    with TestClient(app) as client:
        assert client.post("/webhooks/lazada", content=b"{}").status_code == 404


def test_failed_verification_is_401_and_stores_nothing():
    with TestClient(app) as client:
        assert post_amazon(client, token="wrong").status_code == 401
    assert stored_events() == []


def test_poison_body_is_422_and_stores_nothing():
    with TestClient(app) as client:
        assert post_amazon(client, body=b"not json").status_code == 422
    assert stored_events() == []


def test_sp_api_literal_path_still_wins_over_the_platform_route():
    # /webhooks/sp-api must not be swallowed by /webhooks/{platform}
    with TestClient(app) as client:
        resp = client.post("/webhooks/sp-api", json={
            "seller_id": "S001",
            "event_type": "order_created",
            "payload": {"order_id": "ORD-1"},
        })
    assert resp.status_code == 202
    assert "event_id" in resp.json()


# --- deliveries we acknowledge but do not store ---

def test_untracked_status_is_acked_and_stored_nowhere():
    with TestClient(app) as client:
        resp = post_amazon(client, body=amazon_body(status="Unfulfillable"))
    assert resp.status_code == 202
    assert stored_events() == []


def test_unknown_account_is_acked_not_errored():
    # A 2xx on purpose: retrying cannot help, and a non-2xx would count against
    # Shopee's per-partner push success rate.
    with TestClient(app) as client:
        resp = post_amazon(client, body=amazon_body(seller="A_NOT_CONNECTED"))
    assert resp.status_code == 202
    assert stored_events() == []


# --- the happy path ---

def test_amazon_delivery_stores_event_and_job():
    with TestClient(app) as client:
        assert post_amazon(client).status_code == 202

    events = stored_events()
    assert len(events) == 1
    event = events[0]
    assert event.seller_id == "S001"            # resolved from SellerId, not asserted
    assert event.event_type == EventType.ORDER_PAID.value
    assert event.platform == "amazon"
    assert event.dedup_key == "nid-001"
    assert event.raw_payload["NotificationType"] == "ORDER_CHANGE"

    jobs = stored_jobs()
    assert len(jobs) == 1
    assert jobs[0].event_id == event.id
    assert jobs[0].seller_id == "S001"


def test_shopee_delivery_resolves_shop_id_to_seller():
    with TestClient(app) as client:
        assert post_shopee(client).status_code == 202

    events = stored_events()
    assert len(events) == 1
    assert events[0].seller_id == "S001"
    assert events[0].platform == "shopee"
    assert events[0].event_type == EventType.ORDER_PAID.value


def test_shopee_response_body_is_empty():
    with TestClient(app) as client:
        resp = post_shopee(client)
    assert resp.status_code == 202
    assert resp.content == b""


# --- deduplication, the reason the dedup_key column exists ---

def test_amazon_redelivery_stores_one_event_and_one_job():
    with TestClient(app) as client:
        first = post_amazon(client)
        second = post_amazon(client)          # identical NotificationId
    assert first.status_code == second.status_code == 202
    assert len(stored_events()) == 1
    assert len(stored_jobs()) == 1


def test_shopee_redelivery_stores_one_event_and_one_job():
    # Shopee has no event id at all; the adapter's synthesized key is what
    # makes an identical (ordersn, status, update_time) a no-op.
    with TestClient(app) as client:
        post_shopee(client)
        post_shopee(client)
    assert len(stored_events()) == 1
    assert len(stored_jobs()) == 1


def test_distinct_notifications_are_not_deduplicated():
    with TestClient(app) as client:
        post_amazon(client, body=amazon_body(notification_id="nid-001"))
        post_amazon(client, body=amazon_body(notification_id="nid-002"))
    assert len(stored_events()) == 2
    assert len(stored_jobs()) == 2


def test_same_dedup_key_on_different_platforms_does_not_collide():
    with TestClient(app) as client:
        post_amazon(client, body=amazon_body(notification_id="shared-key"))
        post_shopee(client)
    assert len(stored_events()) == 2


# --- the event/job pair ---

def test_internal_endpoint_also_creates_a_job():
    with TestClient(app) as client:
        resp = client.post("/events", json={
            "seller_id": "S001",
            "event_type": "inventory_low",
            "payload": {"sku": "WIDGET-42", "current_quantity": 3},
        })
    jobs = store.get_jobs_for_event(resp.json()["event_id"])
    assert len(jobs) == 1


def test_internal_events_carry_no_platform_and_never_dedup():
    payload = {
        "seller_id": "S001",
        "event_type": "inventory_low",
        "payload": {"sku": "WIDGET-42", "current_quantity": 3},
    }
    with TestClient(app) as client:
        client.post("/events", json=payload)
        client.post("/events", json=payload)
    events = stored_events()
    assert len(events) == 2                     # a seller may genuinely reorder twice
    assert all(e.platform is None and e.dedup_key is None for e in events)


def test_job_is_closed_once_the_bridge_has_run():
    with TestClient(app) as client:
        resp = client.post("/events", json={
            "seller_id": "S001",
            "event_type": "inventory_low",
            "payload": {"sku": "WIDGET-42", "current_quantity": 3},
        })
    job = store.get_jobs_for_event(resp.json()["event_id"])[0]
    assert job.status == JobStatus.DONE


def test_failed_pipeline_marks_the_job_dead():
    with TestClient(app) as client:
        resp = client.post("/webhooks/sp-api", json={
            "seller_id": "S003",                # inactive seller → pipeline raises
            "event_type": "order_created",
            "payload": {"order_id": "ORD-1"},
        })
    job = store.get_jobs_for_event(resp.json()["event_id"])[0]
    assert job.status == JobStatus.DEAD
    assert "not active" in job.last_error
