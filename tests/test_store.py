from datetime import datetime, timedelta, timezone

from app import store
from app.models.event import EventRecord, EventStatus, EventType

NOW = datetime.now(timezone.utc)


def _make_event(
    event_id: str,
    seller_id: str,
    event_type: EventType,
    payload: dict,
    age: timedelta,
    status: EventStatus = EventStatus.COMPLETED,
) -> EventRecord:
    created_at = NOW - age
    record = EventRecord(
        id=event_id,
        seller_id=seller_id,
        event_type=event_type,
        payload=payload,
        status=status,
        result=None,
        error=None,
        created_at=created_at,
        updated_at=created_at,
    )
    store.create_event(record)
    return record


# --- get_recent_events_for_seller ---

def test_get_recent_events_for_seller_filters_by_window():
    _make_event("ev-recent", "S001", EventType.INVENTORY_LOW, {"sku": "A"}, timedelta(hours=1))
    _make_event("ev-old", "S001", EventType.INVENTORY_LOW, {"sku": "A"}, timedelta(hours=10))

    results = store.get_recent_events_for_seller("S001", hours=4)

    ids = {e.id for e in results}
    assert "ev-recent" in ids
    assert "ev-old" not in ids


def test_get_recent_events_for_seller_excludes_other_sellers():
    _make_event("ev-s001", "S001", EventType.INVENTORY_LOW, {"sku": "A"}, timedelta(hours=1))
    _make_event("ev-s002", "S002", EventType.INVENTORY_LOW, {"sku": "A"}, timedelta(hours=1))

    results = store.get_recent_events_for_seller("S001", hours=4)

    ids = {e.id for e in results}
    assert "ev-s001" in ids
    assert "ev-s002" not in ids


def test_get_recent_events_for_seller_excludes_incomplete():
    _make_event(
        "ev-pending", "S001", EventType.INVENTORY_LOW, {"sku": "A"},
        timedelta(hours=1), status=EventStatus.PENDING,
    )
    _make_event("ev-completed", "S001", EventType.INVENTORY_LOW, {"sku": "A"}, timedelta(hours=1))

    results = store.get_recent_events_for_seller("S001", hours=4)

    ids = {e.id for e in results}
    assert "ev-completed" in ids
    assert "ev-pending" not in ids


def test_get_recent_events_for_seller_orders_desc_and_respects_limit():
    _make_event("ev-1", "S001", EventType.INVENTORY_LOW, {"sku": "A"}, timedelta(hours=3))
    _make_event("ev-2", "S001", EventType.INVENTORY_LOW, {"sku": "A"}, timedelta(hours=2))
    _make_event("ev-3", "S001", EventType.INVENTORY_LOW, {"sku": "A"}, timedelta(hours=1))

    results = store.get_recent_events_for_seller("S001", hours=4, limit=2)

    assert [e.id for e in results] == ["ev-3", "ev-2"]


# --- get_events_by_sku ---

def test_get_events_by_sku_filters_by_sku_and_window():
    _make_event("ev-match", "S001", EventType.INVENTORY_LOW, {"sku": "WIDGET-42"}, timedelta(days=1))
    _make_event("ev-other-sku", "S001", EventType.INVENTORY_LOW, {"sku": "OTHER"}, timedelta(days=1))
    _make_event("ev-too-old", "S001", EventType.INVENTORY_LOW, {"sku": "WIDGET-42"}, timedelta(days=20))

    results = store.get_events_by_sku("S001", "WIDGET-42", days=14)

    ids = {e.id for e in results}
    assert ids == {"ev-match"}


def test_get_events_by_sku_excludes_other_sellers():
    _make_event("ev-s001", "S001", EventType.INVENTORY_LOW, {"sku": "WIDGET-42"}, timedelta(days=1))
    _make_event("ev-s002", "S002", EventType.INVENTORY_LOW, {"sku": "WIDGET-42"}, timedelta(days=1))

    results = store.get_events_by_sku("S001", "WIDGET-42", days=14)

    ids = {e.id for e in results}
    assert "ev-s001" in ids
    assert "ev-s002" not in ids


def test_get_events_by_sku_respects_limit():
    for i in range(3):
        _make_event(f"ev-{i}", "S001", EventType.INVENTORY_LOW, {"sku": "WIDGET-42"}, timedelta(hours=i + 1))

    results = store.get_events_by_sku("S001", "WIDGET-42", days=14, limit=2)

    assert len(results) == 2
