import os
import uuid

import httpx

from app.models.decision import PolicyResult
from app.models.intent import Intent
from app.platforms.amazon import auth


def execute_intent(intent: Intent, seller, payload: dict, policy_result: PolicyResult) -> dict:
    """Route execution to the correct SP API action based on intent."""
    if intent == Intent.REORDER:
        sku = payload.get("sku", "UNKNOWN")
        quantity = policy_result.recommended_quantity or 0
        return _create_restock_order(seller, sku, quantity)

    # Monitoring intents — no SP API action, just acknowledge
    return {"status": "acknowledged", "intent": intent.value}


def _create_restock_order(seller, sku: str, quantity: int) -> dict:
    """Create a restocking order via SP API (or return mock response)."""
    if os.environ.get("SP_API_ENABLED", "false").lower() != "true":
        return {
            "status": "success",
            "order_id": f"MOCK-PO-{uuid.uuid4().hex[:8].upper()}",
            "sku": sku,
            "quantity": quantity,
        }

    creds = auth.amazon_credentials(seller)
    access_token = auth.get_access_token(seller)

    resp = httpx.post(
        f"{creds.endpoint}/vendor/orders/v1/purchaseOrders",
        headers={
            "x-amz-access-token": access_token,
            "Content-Type": "application/json",
        },
        json={
            "purchaseOrderNumber": f"PO-{uuid.uuid4().hex[:8].upper()}",
            "orderDetails": {
                "items": [{"buyerProductIdentifier": sku, "quantity": quantity}],
                "purchaseOrderState": "New",
            },
        },
    )
    resp.raise_for_status()
    return resp.json()
