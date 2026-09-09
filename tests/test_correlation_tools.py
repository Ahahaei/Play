from datetime import datetime, timedelta, timezone

from app import store
from app.llm import correlation_tools as ct
from app.models.approval import ApprovalStatus, PendingApproval
from app.models.decision import (
    DecisionResult,
    ExecutionResult,
    ExecutionStatus,
    PolicyResult,
    RiskLevel,
)
from app.models.event import EventRecord, EventStatus, EventType
from app.models.intent import Intent

NOW = datetime.now(timezone.utc)


def _decision(risk: RiskLevel, status: ExecutionStatus) -> DecisionResult:
    return DecisionResult(
        intent=Intent.REORDER,
        policy_result=PolicyResult(
            action="Reorder 40 units",
            risk_level=risk,
            reasoning="Within auto-approve limits",
            recommended_quantity=40,
            estimated_spend=320.0,
        ),
        execution_result=ExecutionResult(status=status, message="Done"),
    )


def _event(
    event_id: str,
    event_type: EventType = EventType.INVENTORY_LOW,
    payload: dict | None = None,
    age: timedelta = timedelta(hours=1),
    seller_id: str = "S001",
    decision: DecisionResult | None = None,
) -> EventRecord:
    created_at = NOW - age
    store.create_event(EventRecord(
        id=event_id,
        seller_id=seller_id,
        event_type=event_type,
        payload=payload if payload is not None else {"sku": "WIDGET-42", "current_quantity": 3},
        status=EventStatus.PENDING,
        created_at=created_at,
        updated_at=created_at,
    ))
    store.set_event_completed(event_id, decision)
    return store.get_event(event_id)


def _approval(approval_id: str, event_id: str, seller_id: str = "S001") -> None:
    store.create_approval(PendingApproval(
        id=approval_id,
        event_id=event_id,
        seller_id=seller_id,
        intent=Intent.REORDER,
        policy_result=PolicyResult(
            action="Reorder 200 units",
            risk_level=RiskLevel.HIGH,
            reasoning="Exceeds auto-approve spend limit",
            recommended_quantity=200,
            estimated_spend=1000.0,
        ),
        status=ApprovalStatus.PENDING,
        created_at=NOW,
    ))


# --- tool schemas ---

def test_all_six_tools_are_defined():
    names = [t["name"] for t in ct.CORRELATION_TOOLS]
    assert names == [
        "query_events",
        "get_event_detail",
        "get_sku_history",
        "check_pending_approvals",
        "submit_insight",
        "no_insight",
    ]


def test_every_tool_has_name_description_and_schema():
    for tool in ct.CORRELATION_TOOLS:
        assert tool["name"]
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"
        assert "properties" in tool["input_schema"]


def test_terminal_tools_are_marked():
    assert ct.TERMINAL_TOOLS == {"submit_insight", "no_insight"}


def test_submit_insight_severity_enum_matches_constant():
    tool = next(t for t in ct.CORRELATION_TOOLS if t["name"] == "submit_insight")
    assert tool["input_schema"]["properties"]["severity"]["enum"] == list(ct.SEVERITIES)


def test_query_events_type_enum_only_offers_monitoring_types():
    tool = next(t for t in ct.CORRELATION_TOOLS if t["name"] == "query_events")
    assert tool["input_schema"]["properties"]["event_type"]["enum"] == [
        "high_refund_rate_detected",
        "inventory_low",
        "order_spike_detected",
    ]


# --- query_events ---

def test_query_events_empty():
    assert "No completed events" in ct.query_events("S001", hours=24)


def test_query_events_lists_events_with_id_and_type():
    _event("ev-a", EventType.INVENTORY_LOW)
    result = ct.query_events("S001", hours=24)
    assert "[ev-a]" in result
    assert "inventory_low" in result
    assert "sku=WIDGET-42" in result


def test_query_events_includes_risk_and_outcome_when_decided():
    _event("ev-a", decision=_decision(RiskLevel.LOW, ExecutionStatus.EXECUTED))
    result = ct.query_events("S001", hours=24)
    assert "risk=LOW" in result
    assert "outcome=executed" in result


def test_query_events_filters_by_type():
    _event("ev-inv", EventType.INVENTORY_LOW)
    _event("ev-refund", EventType.HIGH_REFUND_RATE_DETECTED, payload={"refund_rate": 0.08})
    result = ct.query_events("S001", event_type="high_refund_rate_detected", hours=24)
    assert "[ev-refund]" in result
    assert "[ev-inv]" not in result


def test_query_events_respects_window():
    _event("ev-old", age=timedelta(hours=30))
    assert "[ev-old]" not in ct.query_events("S001", hours=4)


def test_query_events_is_seller_scoped():
    _event("ev-other", seller_id="S002")
    assert "[ev-other]" not in ct.query_events("S001", hours=24)


# --- get_event_detail ---

def test_get_event_detail_includes_full_payload():
    _event("ev-a", payload={"sku": "WIDGET-42", "current_quantity": 3, "warehouse": "US-EAST"})
    result = ct.get_event_detail("S001", "ev-a")
    assert "warehouse" in result  # omitted from the compact line, present in detail
    assert "US-EAST" in result


def test_get_event_detail_includes_decision_when_present():
    _event("ev-a", decision=_decision(RiskLevel.HIGH, ExecutionStatus.ESCALATED))
    result = ct.get_event_detail("S001", "ev-a")
    assert "Reorder 40 units" in result
    assert "risk=HIGH" in result
    assert "escalated" in result
    assert "$320.00" in result


def test_get_event_detail_unknown_id():
    assert "No event found" in ct.get_event_detail("S001", "does-not-exist")


def test_get_event_detail_refuses_cross_tenant_read():
    _event("ev-s002", seller_id="S002", payload={"sku": "SECRET-SKU"})
    result = ct.get_event_detail("S001", "ev-s002")
    assert "No event found" in result
    assert "SECRET-SKU" not in result


# --- get_sku_history ---

def test_get_sku_history_empty():
    assert "No events recorded" in ct.get_sku_history("S001", "WIDGET-42", days=14)


def test_get_sku_history_counts_by_type():
    _event("ev-1", EventType.INVENTORY_LOW, age=timedelta(days=1))
    _event("ev-2", EventType.INVENTORY_LOW, age=timedelta(days=5))
    _event(
        "ev-3", EventType.HIGH_REFUND_RATE_DETECTED,
        payload={"sku": "WIDGET-42", "refund_rate": 0.08}, age=timedelta(days=2),
    )
    result = ct.get_sku_history("S001", "WIDGET-42", days=14)
    assert "3 event(s) for WIDGET-42" in result
    assert "inventory_low x2" in result
    assert "high_refund_rate_detected x1" in result


def test_get_sku_history_excludes_other_skus():
    _event("ev-other", payload={"sku": "OTHER-SKU"}, age=timedelta(days=1))
    assert "No events recorded" in ct.get_sku_history("S001", "WIDGET-42", days=14)


def test_get_sku_history_respects_day_window():
    _event("ev-ancient", age=timedelta(days=30))
    assert "No events recorded" in ct.get_sku_history("S001", "WIDGET-42", days=14)


# --- check_pending_approvals ---

def test_check_pending_approvals_empty():
    assert "No pending approvals" in ct.check_pending_approvals("S001")


def test_check_pending_approvals_lists_with_sku_and_spend():
    _event("ev-a", payload={"sku": "BULK-01", "current_quantity": 5})
    _approval("ap-1", "ev-a")
    result = ct.check_pending_approvals("S001")
    assert "[ap-1]" in result
    assert "sku=BULK-01" in result
    assert "$1,000.00" in result
    assert "risk=HIGH" in result


def test_check_pending_approvals_filters_by_sku():
    _event("ev-a", payload={"sku": "BULK-01"})
    _event("ev-b", payload={"sku": "WIDGET-42"})
    _approval("ap-bulk", "ev-a")
    _approval("ap-widget", "ev-b")

    result = ct.check_pending_approvals("S001", sku="WIDGET-42")

    assert "[ap-widget]" in result
    assert "[ap-bulk]" not in result


def test_check_pending_approvals_no_match_for_sku():
    _event("ev-a", payload={"sku": "BULK-01"})
    _approval("ap-1", "ev-a")
    assert "No pending approvals for WIDGET-42" in ct.check_pending_approvals("S001", sku="WIDGET-42")


def test_check_pending_approvals_is_seller_scoped():
    _event("ev-s002", seller_id="S002", payload={"sku": "BULK-01"})
    _approval("ap-s002", "ev-s002", seller_id="S002")
    assert "No pending approvals" in ct.check_pending_approvals("S001")


# --- execute_tool dispatch ---

def test_execute_tool_dispatches_query_events():
    _event("ev-a")
    assert "[ev-a]" in ct.execute_tool("query_events", {"hours": 24}, "S001")


def test_execute_tool_applies_defaults_for_omitted_args():
    _event("ev-a")
    assert "[ev-a]" in ct.execute_tool("query_events", {}, "S001")


def test_execute_tool_unknown_tool():
    assert "Unknown tool" in ct.execute_tool("drop_tables", {}, "S001")


def test_execute_tool_rejects_terminal_tools():
    # Terminal tools are the graph's business — dispatching one here is a bug, not a query.
    assert "Unknown tool" in ct.execute_tool("submit_insight", {}, "S001")


def test_execute_tool_bad_arguments_returns_text_not_raise():
    result = ct.execute_tool("get_sku_history", {"wrong_arg": 1}, "S001")
    assert "Invalid arguments" in result


def test_execute_tool_cannot_be_told_which_seller_to_read():
    # seller_id comes from the pipeline, never from LLM-supplied input.
    _event("ev-s002", seller_id="S002")
    result = ct.execute_tool("query_events", {"seller_id": "S002"}, "S001")
    assert "Invalid arguments" in result
    assert "[ev-s002]" not in result
