"""
Investigation tools for the cross-event correlation agent.

These are read-only — they surface evidence from the events and approvals tables so the
agent can correlate signals across event types. Nothing here executes a decision, and there
is deliberately no path from this module to any write: the agent can look, never touch.

Two consumers, one source of truth for the tool descriptions:
  - `execute_tool` — plain dispatch, used by tests and any raw-Anthropic-SDK caller.
  - `build_tools(seller_id)` — the same four handlers wrapped as LangChain tools for the
    deepagents harness, with seller_id bound by closure so the model can never supply it.
"""
import json
import logging
from typing import Literal, Optional

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app import store
from app.models.event import MONITORING_EVENT_TYPES, EventRecord

logger = logging.getLogger(__name__)

SEVERITIES = ("info", "warning", "critical")

CORRELATION_TOOLS: list[dict] = [
    {
        "name": "query_events",
        "description": (
            "Query the seller's recent completed events within a time window. "
            "Omit event_type to see every monitoring signal that fired — this is the usual "
            "starting point for an investigation. Returns one line per event with its ID, "
            "timestamp, type, SKU, risk level and outcome."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "enum": sorted(t.value for t in MONITORING_EVENT_TYPES),
                    "description": "Restrict to a single monitoring event type. Omit for all types.",
                },
                "hours": {
                    "type": "integer",
                    "description": "How many hours back to look. Defaults to 24.",
                },
            },
        },
    },
    {
        "name": "get_event_detail",
        "description": (
            "Fetch the full payload and decision outcome of one event. Use this after "
            "query_events or get_sku_history surfaces an event worth drilling into — the "
            "summary lines omit most payload fields."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "The event ID, exactly as shown in square brackets in a previous result.",
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "get_sku_history",
        "description": (
            "List every event recorded for a single SKU over a window measured in days. "
            "Use this to check whether the current signal is a one-off or a recurring pattern — "
            "a short hourly window will not reveal that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {
                    "type": "string",
                    "description": "The product SKU, e.g. WIDGET-42",
                },
                "days": {
                    "type": "integer",
                    "description": "How many days back to look. Defaults to 14.",
                },
            },
            "required": ["sku"],
        },
    },
    {
        "name": "check_pending_approvals",
        "description": (
            "List approvals still awaiting the seller's decision, optionally narrowed to one SKU. "
            "Use this to find out whether an action is already in flight before recommending "
            "anything about it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {
                    "type": "string",
                    "description": "Only return approvals for this SKU. Omit for all pending approvals.",
                },
            },
        },
    },
]


class InvestigationVerdict(BaseModel):
    """
    The agent's final answer, returned as structured output rather than via a terminal tool.

    The deepagents harness ends a run when the model stops calling tools, and RubricMiddleware
    grades the transcript at exactly that moment. A terminal tool call would mean the agent
    never "finished", so the validator would never fire — which is why the earlier
    submit_insight / no_insight tools were replaced by this model. `should_report=False` is
    the old no_insight.
    """

    should_report: bool = Field(
        description=(
            "True only if the evidence shows something no single event reveals on its own. "
            "False when the signals are unrelated, routine, or independently explained — "
            "which is the correct outcome most of the time."
        )
    )
    severity: Literal["info", "warning", "critical"] = Field(
        default="info",
        description=(
            "info — worth knowing; warning — needs attention soon; "
            "critical — acting on the wrong assumption right now costs money."
        ),
    )
    summary: str = Field(
        default="",
        description=(
            "The finding in plain language: what the signals are, why they are connected, and "
            "what the seller should do about it. Empty when should_report is False."
        ),
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of the events this conclusion rests on, exactly as they appeared in tool "
            "results. Never invent an ID."
        ),
    )
    no_report_reason: str = Field(
        default="",
        description=(
            "When should_report is False, why. Recorded for evaluation, never sent to the seller."
        ),
    )

# Payload keys rendered in the compact event lines; everything else needs get_event_detail.
_SUMMARY_KEYS = (
    "current_quantity",
    "requested_quantity",
    "refund_rate",
    "refund_count",
    "order_count",
    "total_orders",
    "baseline_orders",
    "current_orders",
)


def _payload_summary(payload: dict) -> str:
    parts = [f"{k}={payload[k]}" for k in _SUMMARY_KEYS if k in payload]
    return " ".join(parts)


def _format_event_line(event: EventRecord) -> str:
    fields = [
        f"[{event.id}]",
        event.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        event.event_type.value,
        f"sku={event.payload.get('sku', '—')}",
    ]
    summary = _payload_summary(event.payload)
    if summary:
        fields.append(summary)
    if event.result is not None:
        fields.append(f"risk={event.result.policy_result.risk_level.value}")
        fields.append(f"outcome={event.result.execution_result.status.value}")
    return " | ".join(fields)


def query_events(
    seller_id: str, event_type: Optional[str] = None, hours: int = 24
) -> str:
    events = store.get_recent_events_for_seller(seller_id, hours=hours, limit=40)
    if event_type is not None:
        events = [e for e in events if e.event_type.value == event_type]

    scope = event_type or "all types"
    if not events:
        return f"No completed events ({scope}) in the last {hours}h."

    lines = [f"{len(events)} event(s) ({scope}) in the last {hours}h, newest first:"]
    lines.extend(_format_event_line(e) for e in events)
    return "\n".join(lines)


def get_event_detail(seller_id: str, event_id: str) -> str:
    event = store.get_event(event_id)
    # Never let a hallucinated or cross-tenant ID leak another seller's data.
    if event is None or event.seller_id != seller_id:
        return f"No event found with ID {event_id}."

    lines = [
        f"Event {event.id}",
        f"type: {event.event_type.value}",
        f"recorded: {event.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"status: {event.status.value}",
        f"payload: {json.dumps(event.payload, sort_keys=True)}",
    ]
    if event.result is not None:
        pr = event.result.policy_result
        er = event.result.execution_result
        lines.extend([
            f"intent: {event.result.intent.value}",
            f"decision: {pr.action} (risk={pr.risk_level.value})",
            f"reasoning: {pr.reasoning}",
            f"outcome: {er.status.value} — {er.message}",
        ])
        if pr.estimated_spend is not None:
            lines.append(f"estimated_spend: ${pr.estimated_spend:,.2f}")
    return "\n".join(lines)


def get_sku_history(seller_id: str, sku: str, days: int = 14) -> str:
    events = store.get_events_by_sku(seller_id, sku, days=days, limit=40)
    if not events:
        return f"No events recorded for {sku} in the last {days} days."

    counts: dict[str, int] = {}
    for e in events:
        counts[e.event_type.value] = counts.get(e.event_type.value, 0) + 1
    breakdown = ", ".join(f"{t} x{n}" for t, n in sorted(counts.items()))

    lines = [
        f"{len(events)} event(s) for {sku} in the last {days} days ({breakdown}), newest first:"
    ]
    lines.extend(_format_event_line(e) for e in events)
    return "\n".join(lines)


def check_pending_approvals(seller_id: str, sku: Optional[str] = None) -> str:
    approvals = store.get_pending_approvals_for_seller(seller_id)

    rows = []
    for approval in approvals:
        event = store.get_event(approval.event_id)
        approval_sku = event.payload.get("sku") if event else None
        if sku is not None and approval_sku != sku:
            continue
        rows.append((approval, approval_sku))

    scope = f" for {sku}" if sku else ""
    if not rows:
        return f"No pending approvals{scope}."

    lines = [f"{len(rows)} pending approval(s){scope}, newest first:"]
    for approval, approval_sku in rows:
        pr = approval.policy_result
        line = (
            f"[{approval.id}] {approval.created_at.strftime('%Y-%m-%d %H:%M UTC')} | "
            f"{pr.action} | sku={approval_sku or '—'} | risk={pr.risk_level.value} | "
            f"event={approval.event_id}"
        )
        if pr.estimated_spend is not None:
            line += f" | est_spend=${pr.estimated_spend:,.2f}"
        lines.append(line)
    return "\n".join(lines)


_HANDLERS = {
    "query_events": query_events,
    "get_event_detail": get_event_detail,
    "get_sku_history": get_sku_history,
    "check_pending_approvals": check_pending_approvals,
}


def execute_tool(name: str, tool_input: dict, seller_id: str) -> str:
    """
    Run one investigation tool and return its result as text for the LLM.

    Terminal tools are not dispatched here — the graph routes those to its conclude node.
    Failures come back as text rather than raising: a tool that errors should cost the agent
    one turn, not abort the whole investigation.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool '{name}'."

    try:
        return handler(seller_id, **tool_input)
    except TypeError as exc:
        logger.warning("seller=%s tool=%s bad arguments: %s", seller_id, name, exc)
        return f"Invalid arguments for '{name}': {exc}"
    except Exception as exc:
        logger.exception("seller=%s tool=%s failed: %s", seller_id, name, exc)
        return f"Tool '{name}' failed: {exc}"


def build_tools(seller_id: str) -> list[BaseTool]:
    """
    Wrap the four investigation handlers as LangChain tools for the deepagents harness.

    `seller_id` is bound here by closure and appears in no tool schema, so the model has no
    way to ask for another tenant's data — the same guarantee `execute_tool` gets by keeping
    seller_id positional. Descriptions come from CORRELATION_TOOLS so the two call paths
    can never drift apart. Build a fresh list per investigation.
    """
    descriptions = {tool["name"]: tool["description"] for tool in CORRELATION_TOOLS}

    def _query_events(event_type: Optional[str] = None, hours: int = 24) -> str:
        return query_events(seller_id, event_type=event_type, hours=hours)

    def _get_event_detail(event_id: str) -> str:
        return get_event_detail(seller_id, event_id)

    def _get_sku_history(sku: str, days: int = 14) -> str:
        return get_sku_history(seller_id, sku, days=days)

    def _check_pending_approvals(sku: Optional[str] = None) -> str:
        return check_pending_approvals(seller_id, sku=sku)

    return [
        StructuredTool.from_function(func=fn, name=name, description=descriptions[name])
        for name, fn in (
            ("query_events", _query_events),
            ("get_event_detail", _get_event_detail),
            ("get_sku_history", _get_sku_history),
            ("check_pending_approvals", _check_pending_approvals),
        )
    ]
