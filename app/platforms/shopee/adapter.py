"""Shopee Open API inbound adapter.

Shopee genuinely HTTP-POSTs to a callback URL registered in the Open Platform
Console — per app, not per seller — and expects 2xx with an empty body.

Two things make this adapter differ from Amazon's in kind, not just in field
names:

  * **No event id anywhere in the envelope.** A redelivery is indistinguishable
    from the original except by identical `(ordersn, status, update_time)`, so
    the dedup key is synthesized. It accepts a small collision risk.
  * **One integer `code=3` covers created/paid/shipped/canceled**, told apart
    only by `data.status`.
"""
import hashlib
import hmac
import json
import logging
import os
from typing import Optional

from app.models.event import EventType
from app.platforms.base import NormalizationError, NormalizedEvent, RawDelivery

logger = logging.getLogger(__name__)

_ORDER_STATUS_PUSH = 3

# data.status → internal event. Shopee's enum is wider than ours; everything
# unmapped is a real status we do not track. IN_CANCEL is a cancellation
# *request*, not a cancellation, so it is not order_canceled.
_STATUS_MAP: dict[str, Optional[EventType]] = {
    "UNPAID": EventType.ORDER_CREATED,
    "READY_TO_SHIP": EventType.ORDER_PAID,
    "SHIPPED": EventType.ORDER_SHIPPED,
    "CANCELLED": EventType.ORDER_CANCELED,
    "RETRY_SHIP": None,
    "PROCESSED": None,
    "TO_CONFIRM_RECEIVE": None,
    "IN_CANCEL": None,
    "TO_RETURN": None,
    "COMPLETED": None,
}


def push_signature(url: str, body: bytes, partner_key: str) -> str:
    """HMAC-SHA256 over `url + body`, hex-encoded.

    ⚠ Verify this base string against the Open Platform Console before going
    live. Shopee's official docs render behind JavaScript; this construction
    comes from official-linked SDK examples. Note that a widely-circulated
    third-party tutorial describing an `x-shopee-signature` header is wrong —
    the signature arrives in `Authorization`.
    """
    base = url.encode() + body
    return hmac.new(partner_key.encode(), base, hashlib.sha256).hexdigest()


class ShopeeInboundAdapter:
    name = "shopee"

    def verify(self, raw: RawDelivery) -> bool:
        # partner_key is per *application*, not per shop, so it comes from
        # config — the shop cannot be known before the body is parsed anyway.
        partner_key = os.environ.get("SHOPEE_PARTNER_KEY", "")
        if not partner_key:
            logger.warning("SHOPEE_PARTNER_KEY is unset — rejecting shopee delivery")
            return False
        provided = raw.header("Authorization") or ""
        return hmac.compare_digest(push_signature(raw.url, raw.body, partner_key), provided)

    def normalize(self, raw: RawDelivery) -> list[NormalizedEvent]:
        try:
            envelope = json.loads(raw.body)
        except (ValueError, TypeError) as exc:
            raise NormalizationError(f"shopee: body is not JSON: {exc}") from exc
        if not isinstance(envelope, dict):
            raise NormalizationError("shopee: envelope is not an object")

        if envelope.get("code") != _ORDER_STATUS_PUSH:
            # code=1 is shop authorization; 2 and 4-12 are unconfirmed. Ignored.
            return []

        shop_id = envelope.get("shop_id")
        if shop_id is None:
            raise NormalizationError("shopee: order push carries no shop_id")

        data = envelope.get("data")
        if not isinstance(data, dict):
            raise NormalizationError("shopee: code=3 push without a data object")

        status = data.get("status")
        event_type = _STATUS_MAP.get(status)
        if event_type is None:
            return []

        ordersn = data.get("ordersn")
        if not ordersn:
            raise NormalizationError("shopee: order push carries no ordersn")

        update_time = data.get("update_time", envelope.get("timestamp"))
        dedup_key = hashlib.sha256(
            f"{shop_id}|{envelope['code']}|{ordersn}|{status}|{update_time}".encode()
        ).hexdigest()

        payload = {
            "order_id": ordersn,
            "status": status,
            "update_time": update_time,
            # data.items is frequently [] — enrichment via get_order_detail is
            # required before this is an adequate audit record (FIX.MD A4).
            "items": data.get("items") or [],
        }

        return [NormalizedEvent(
            external_id=str(shop_id),
            event_type=event_type,
            payload=payload,
            dedup_key=dedup_key,
            raw_payload=envelope,
        )]
