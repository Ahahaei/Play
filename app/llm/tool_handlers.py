import logging

from app import store
from app.engine.pipeline import run_job
from app.models.decision import ExecutionStatus
from app.models.event import EventStatus, EventType
from app.models.seller import Seller

logger = logging.getLogger(__name__)


def reorder_sku(sku: str, quantity: int, seller: Seller) -> str:
    """
    Inject a manual reorder into the pipeline.
    The policy engine evaluates it against the seller's thresholds — auto-execute or escalate.
    """
    # Entry 2 of the ingest diagram: the same event + job insert pair the
    # platform route performs. Still run inline — Stage 4 hands it to the
    # worker and replies with an acknowledgement instead of a result.
    enqueued = store.ingest_internal_event(
        seller.id,
        EventType.INVENTORY_LOW,
        # requested_quantity overrides the seller's default reorder_quantity in the policy engine
        {"sku": sku, "current_quantity": 0, "requested_quantity": quantity},
    )
    run_job(enqueued.job_id, enqueued.event_id)

    event = store.get_event(enqueued.event_id)
    if event.status == EventStatus.FAILED:
        return f"Failed to process reorder: {event.error}"

    result = event.result
    execution_status = result.execution_result.status

    if execution_status == ExecutionStatus.EXECUTED:
        sp_result = result.execution_result.sp_api_result or {}
        order_id = sp_result.get("order_id", "N/A")
        spend = result.policy_result.estimated_spend
        return (
            f"Reorder submitted: {quantity} units of {sku}. "
            f"Order ID: {order_id}. Est. spend: ${spend:,.2f}."
        )

    if execution_status == ExecutionStatus.ESCALATED:
        spend = result.policy_result.estimated_spend
        return (
            f"Reorder of {quantity} units of {sku} (est. ${spend:,.2f}) "
            f"exceeds auto-approve limits — sent to your channel for approval."
        )

    return "Reorder processed."


def list_approvals(seller: Seller) -> str:
    """Return a formatted list of pending approvals for this seller."""
    approvals = store.get_pending_approvals_for_seller(seller.id)
    if not approvals:
        return "No pending approvals."

    lines = [f"{len(approvals)} pending approval(s):"]
    for a in approvals:
        pr = a.policy_result
        spend_part = f" — est. ${pr.estimated_spend:,.2f}" if pr.estimated_spend else ""
        lines.append(f"• {pr.action}{spend_part} (ID: {a.id[:8]}...)")
    return "\n".join(lines)


def get_refund_rate(seller: Seller) -> str:
    """Return the most recently recorded refund rate for this seller."""
    events = store.get_recent_events_by_type(
        seller.id, EventType.HIGH_REFUND_RATE_DETECTED, limit=1
    )
    if not events:
        return "No refund rate data recorded yet."

    payload = events[0].payload
    refund_count = payload.get("refund_count", 0)
    order_count = payload.get("order_count", 1)
    rate = (refund_count / order_count * 100) if order_count > 0 else 0.0
    window = payload.get("window_minutes", 1440)
    return (
        f"Refund rate: {rate:.1f}% "
        f"({refund_count} refunds / {order_count} orders in last {window} min)."
    )
