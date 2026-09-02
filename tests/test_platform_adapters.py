"""Phase A / A2, A3, A5 — the inbound platform boundary.

Payloads are the documented shapes from the SP-API / Shopee comparison
(retrieved 2026-08-31), not invented ones.
"""
import hashlib
import json

import pytest

from app.models.event import EventType
from app.platforms import registry
from app.platforms.amazon.adapter import AmazonInboundAdapter
from app.platforms.base import NormalizationError, RawDelivery, UnknownPlatform
from app.platforms.shopee.adapter import ShopeeInboundAdapter, push_signature

AMAZON = AmazonInboundAdapter()
SHOPEE = ShopeeInboundAdapter()

SHOPEE_URL = "https://ops.example.com/webhooks/shopee"
PARTNER_KEY = "test-partner-key"


def amazon_envelope(status="Unshipped", notification_id="52ac10-abc", **over):
    envelope = {
        "NotificationVersion": "1.0",
        "NotificationType": "ORDER_CHANGE",
        "PayloadVersion": "1.0",
        "EventTime": "2023-10-03T01:35:06.382Z",
        "Payload": {"OrderChangeNotification": {
            "NotificationLevel": "OrderLevel",
            "SellerId": "ABCDEFGFMDKELDW",
            "AmazonOrderId": "123-4567891-4567891",
            "OrderChangeType": "OrderStatusChange",
            "Summary": {
                "MarketplaceId": "A2Q3Y263D00KWC",
                "OrderStatus": status,
                "PurchaseDate": "2023-10-03T01:03:44.106Z",
                "FulfillmentType": "MFN",
                "OrderItems": [
                    {"OrderItemId": "12345207241", "SellerSKU": "SKU123", "Quantity": 15}
                ],
            },
        }},
        "NotificationMetadata": {
            "ApplicationId": "amzn1.sp.solution.c4d",
            "SubscriptionId": "52ac10",
            "PublishTime": "2023-10-03T01:35:07.931Z",
            "NotificationId": notification_id,
        },
    }
    envelope.update(over)
    return envelope


def amazon_delivery(envelope=None, token="s3cret"):
    body = json.dumps(envelope if envelope is not None else amazon_envelope()).encode()
    return RawDelivery(body=body, headers={"X-Internal-Token": token})


def shopee_envelope(status="READY_TO_SHIP", code=3, **over):
    envelope = {
        "data": {
            "items": [],
            "ordersn": "2112132KAD867D",
            "status": status,
            "update_time": 1639390316,
        },
        "shop_id": 30011,
        "code": code,
        "timestamp": 1639390316,
    }
    envelope.update(over)
    return envelope


def shopee_delivery(envelope=None, key=PARTNER_KEY, url=SHOPEE_URL):
    body = json.dumps(envelope if envelope is not None else shopee_envelope()).encode()
    return RawDelivery(
        body=body,
        headers={"Authorization": push_signature(url, body, key)},
        url=url,
    )


@pytest.fixture(autouse=True)
def platform_secrets(monkeypatch):
    monkeypatch.setenv("INTERNAL_INGEST_SECRET", "s3cret")
    monkeypatch.setenv("SHOPEE_PARTNER_KEY", PARTNER_KEY)


# --- registry ---

def test_registry_resolves_both_platforms():
    assert registry.get("amazon").name == "amazon"
    assert registry.get("shopee").name == "shopee"


def test_registry_rejects_unknown_platform():
    with pytest.raises(UnknownPlatform):
        registry.get("lazada")


# --- amazon verify ---

def test_amazon_accepts_correct_secret():
    assert AMAZON.verify(amazon_delivery()) is True


def test_amazon_rejects_wrong_secret():
    assert AMAZON.verify(amazon_delivery(token="wrong")) is False


def test_amazon_rejects_missing_header():
    assert AMAZON.verify(RawDelivery(body=b"{}")) is False


def test_amazon_fails_closed_when_secret_unconfigured(monkeypatch):
    monkeypatch.delenv("INTERNAL_INGEST_SECRET")
    assert AMAZON.verify(amazon_delivery()) is False


def test_amazon_header_lookup_is_case_insensitive():
    body = json.dumps(amazon_envelope()).encode()
    assert AMAZON.verify(RawDelivery(body=body, headers={"x-internal-token": "s3cret"})) is True


# --- amazon normalize ---

def test_amazon_unshipped_maps_to_order_paid():
    events = AMAZON.normalize(amazon_delivery())
    assert len(events) == 1
    event = events[0]
    assert event.event_type == EventType.ORDER_PAID
    assert event.external_id == "ABCDEFGFMDKELDW"
    assert event.payload["order_id"] == "123-4567891-4567891"
    assert event.payload["items"] == [{"sku": "SKU123", "quantity": 15}]


def test_amazon_dedup_key_is_the_notification_id():
    events = AMAZON.normalize(amazon_delivery(amazon_envelope(notification_id="uuid-1")))
    assert events[0].dedup_key == "uuid-1"


def test_amazon_redelivery_carries_the_same_dedup_key():
    first = AMAZON.normalize(amazon_delivery())[0]
    second = AMAZON.normalize(amazon_delivery())[0]
    assert first.dedup_key == second.dedup_key


@pytest.mark.parametrize("status,expected", [
    ("Pending", EventType.ORDER_CREATED),
    ("Unshipped", EventType.ORDER_PAID),
    ("Shipped", EventType.ORDER_SHIPPED),
    ("Canceled", EventType.ORDER_CANCELED),
])
def test_amazon_status_map(status, expected):
    events = AMAZON.normalize(amazon_delivery(amazon_envelope(status=status)))
    assert events[0].event_type == expected


@pytest.mark.parametrize("status", ["PartiallyShipped", "Unfulfillable", "SomethingNew"])
def test_amazon_untracked_status_yields_nothing(status):
    assert AMAZON.normalize(amazon_delivery(amazon_envelope(status=status))) == []


def test_amazon_other_notification_type_is_ignored_not_an_error():
    envelope = amazon_envelope()
    envelope["NotificationType"] = "FEED_PROCESSING_FINISHED"
    assert AMAZON.normalize(amazon_delivery(envelope)) == []


def test_amazon_raw_payload_is_kept_whole():
    envelope = amazon_envelope()
    assert AMAZON.normalize(amazon_delivery(envelope))[0].raw_payload == envelope


def test_amazon_missing_notification_id_is_terminal():
    envelope = amazon_envelope()
    envelope["NotificationMetadata"]["NotificationId"] = None
    with pytest.raises(NormalizationError):
        AMAZON.normalize(amazon_delivery(envelope))


def test_amazon_missing_seller_id_is_terminal():
    envelope = amazon_envelope()
    del envelope["Payload"]["OrderChangeNotification"]["SellerId"]
    with pytest.raises(NormalizationError):
        AMAZON.normalize(amazon_delivery(envelope))


def test_amazon_unparseable_body_is_terminal():
    with pytest.raises(NormalizationError):
        AMAZON.normalize(RawDelivery(body=b"not json"))


# --- shopee verify ---

def test_shopee_accepts_valid_signature():
    assert SHOPEE.verify(shopee_delivery()) is True


def test_shopee_rejects_tampered_body():
    delivery = shopee_delivery()
    tampered = RawDelivery(
        body=delivery.body.replace(b"30011", b"99999"),
        headers=delivery.headers,
        url=delivery.url,
    )
    assert SHOPEE.verify(tampered) is False


def test_shopee_rejects_signature_from_another_partner_key():
    assert SHOPEE.verify(shopee_delivery(key="someone-elses-key")) is False


def test_shopee_signature_covers_the_url():
    delivery = shopee_delivery(url="https://ops.example.com/webhooks/shopee")
    replayed = RawDelivery(
        body=delivery.body,
        headers=delivery.headers,
        url="https://evil.example.com/webhooks/shopee",
    )
    assert SHOPEE.verify(replayed) is False


def test_shopee_fails_closed_when_partner_key_unconfigured(monkeypatch):
    monkeypatch.delenv("SHOPEE_PARTNER_KEY")
    assert SHOPEE.verify(shopee_delivery()) is False


# --- shopee normalize ---

def test_shopee_ready_to_ship_maps_to_order_paid():
    events = SHOPEE.normalize(shopee_delivery())
    assert len(events) == 1
    event = events[0]
    assert event.event_type == EventType.ORDER_PAID
    assert event.external_id == "30011"
    assert event.payload["order_id"] == "2112132KAD867D"


@pytest.mark.parametrize("status,expected", [
    ("UNPAID", EventType.ORDER_CREATED),
    ("READY_TO_SHIP", EventType.ORDER_PAID),
    ("SHIPPED", EventType.ORDER_SHIPPED),
    ("CANCELLED", EventType.ORDER_CANCELED),
])
def test_shopee_one_code_covers_four_internal_events(status, expected):
    events = SHOPEE.normalize(shopee_delivery(shopee_envelope(status=status)))
    assert events[0].event_type == expected


@pytest.mark.parametrize("status", ["IN_CANCEL", "PROCESSED", "COMPLETED", "TO_RETURN"])
def test_shopee_untracked_status_yields_nothing(status):
    assert SHOPEE.normalize(shopee_delivery(shopee_envelope(status=status))) == []


def test_shopee_non_order_code_is_ignored():
    # code=1 is shop authorization, which carries no `data` at all.
    assert SHOPEE.normalize(shopee_delivery(shopee_envelope(code=1))) == []


def test_shopee_dedup_key_is_synthesized_and_deterministic():
    first = SHOPEE.normalize(shopee_delivery())[0]
    second = SHOPEE.normalize(shopee_delivery())[0]
    expected = hashlib.sha256(
        b"30011|3|2112132KAD867D|READY_TO_SHIP|1639390316"
    ).hexdigest()
    assert first.dedup_key == second.dedup_key == expected


def test_shopee_dedup_key_changes_with_status():
    paid = SHOPEE.normalize(shopee_delivery(shopee_envelope(status="READY_TO_SHIP")))[0]
    shipped = SHOPEE.normalize(shopee_delivery(shopee_envelope(status="SHIPPED")))[0]
    assert paid.dedup_key != shipped.dedup_key


def test_shopee_empty_items_survive_for_later_enrichment():
    assert SHOPEE.normalize(shopee_delivery())[0].payload["items"] == []


def test_shopee_missing_ordersn_is_terminal():
    envelope = shopee_envelope()
    del envelope["data"]["ordersn"]
    with pytest.raises(NormalizationError):
        SHOPEE.normalize(shopee_delivery(envelope))


def test_shopee_code_3_without_data_is_terminal():
    envelope = shopee_envelope()
    envelope["data"] = None
    with pytest.raises(NormalizationError):
        SHOPEE.normalize(shopee_delivery(envelope))


def test_shopee_unparseable_body_is_terminal():
    with pytest.raises(NormalizationError):
        SHOPEE.normalize(RawDelivery(body=b"<html>", url=SHOPEE_URL))
