"""Amazon SP-API inbound adapter.

⚠ Transport caveat: Amazon does **not** HTTP-POST to us. It delivers to an SQS
queue or EventBridge bus we own and we poll it. Until that consumer exists,
deliveries reach this adapter over HTTP from our own poster, and `verify`
checks a shared secret rather than anything Amazon signed. `normalize` parses
the genuine `ORDER_CHANGE` envelope, so the future consumer is a new
`RawDelivery` source and nothing else changes.
"""
import hmac
import json
import logging
import os
from typing import Optional

from app.models.event import EventType
from app.platforms.base import NormalizationError, NormalizedEvent, RawDelivery

logger = logging.getLogger(__name__)

# Summary.OrderStatus → internal event. Statuses mapping to None are real
# transitions we deliberately do not track: PartiallyShipped would fire a second
# order_shipped for one order, and Unfulfillable is not a cancellation.
_STATUS_MAP: dict[str, Optional[EventType]] = {
    "Pending": EventType.ORDER_CREATED,
    "Unshipped": EventType.ORDER_PAID,
    "PartiallyShipped": None,
    "Shipped": EventType.ORDER_SHIPPED,
    "Canceled": EventType.ORDER_CANCELED,
    "Unfulfillable": None,
}


class AmazonInboundAdapter:
    name = "amazon"

    def verify(self, raw: RawDelivery) -> bool:
        """Shared-secret check, standing in for SQS ownership.

        Fails closed: no configured secret means no accepted delivery.
        """
        expected = os.environ.get("INTERNAL_INGEST_SECRET", "")
        if not expected:
            logger.warning("INTERNAL_INGEST_SECRET is unset — rejecting amazon delivery")
            return False
        provided = raw.header("X-Internal-Token") or ""
        return hmac.compare_digest(expected, provided)

    def normalize(self, raw: RawDelivery) -> list[NormalizedEvent]:
        try:
            envelope = json.loads(raw.body)
        except (ValueError, TypeError) as exc:
            raise NormalizationError(f"amazon: body is not JSON: {exc}") from exc
        if not isinstance(envelope, dict):
            raise NormalizationError("amazon: envelope is not an object")

        if envelope.get("NotificationType") != "ORDER_CHANGE":
            # Other notification types are not subscribed to yet. Not an error.
            return []

        notification = (envelope.get("Payload") or {}).get("OrderChangeNotification")
        if not isinstance(notification, dict):
            raise NormalizationError("amazon: ORDER_CHANGE without OrderChangeNotification")

        seller_id = notification.get("SellerId")
        if not seller_id:
            raise NormalizationError("amazon: notification carries no SellerId")

        dedup_key = (envelope.get("NotificationMetadata") or {}).get("NotificationId")
        if not dedup_key:
            # Amazon's documented dedup handle. Without it a redelivery is
            # indistinguishable from a new event, so refuse rather than invent one.
            raise NormalizationError("amazon: notification carries no NotificationId")

        summary = notification.get("Summary") or {}
        event_type = _STATUS_MAP.get(summary.get("OrderStatus"))
        if event_type is None:
            return []

        payload = {
            "order_id": notification.get("AmazonOrderId"),
            "status": summary.get("OrderStatus"),
            "marketplace_id": summary.get("MarketplaceId"),
            "purchase_date": summary.get("PurchaseDate"),
            "fulfillment_type": summary.get("FulfillmentType"),
            "event_time": envelope.get("EventTime"),
            "items": [
                {"sku": item.get("SellerSKU"), "quantity": item.get("Quantity")}
                for item in summary.get("OrderItems") or []
            ],
        }

        return [NormalizedEvent(
            external_id=str(seller_id),
            event_type=event_type,
            payload=payload,
            dedup_key=str(dedup_key),
            raw_payload=envelope,
        )]
