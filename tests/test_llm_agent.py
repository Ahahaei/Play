"""
Phase 3 — LLM agent tests.

All tests mock the Anthropic client. No real API calls are made.
Tool handler tests use the in-memory SQLite DB from conftest.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.store as store
from app.llm import agent as agent_module
from app.llm import tool_handlers
from app.mock.sellers import MOCK_SELLERS
from main import app

# Seller S001 — LOW risk reorders (40 units * $8 = $320, within limits)
S001 = MOCK_SELLERS[0]
# Seller S002 — HIGH risk reorders (200 units * $5 = $1000, above limits)
S002 = MOCK_SELLERS[1]


# ---------------------------------------------------------------------------
# Helpers: build mock Anthropic responses
# ---------------------------------------------------------------------------

def _make_tool_use_response(tool_name: str, tool_input: dict, tool_id: str = "tu_001"):
    """Mock a response where Claude chose to call a tool."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = tool_id
    tool_block.name = tool_name
    tool_block.input = tool_input

    response = MagicMock()
    response.stop_reason = "tool_use"
    response.content = [tool_block]
    return response


def _make_text_response(text: str):
    """Mock a response where Claude replied with plain text."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text

    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [text_block]
    return response


# ---------------------------------------------------------------------------
# agent.run_agent — tool dispatch
# ---------------------------------------------------------------------------

@patch("app.llm.agent._get_client")
def test_agent_dispatches_reorder_sku(mock_get_client):
    tool_response = _make_tool_use_response("reorder_sku", {"sku": "WIDGET-42", "quantity": 10})
    followup = _make_text_response("Done! Reorder placed for 10 units of WIDGET-42.")

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [tool_response, followup]
    mock_get_client.return_value = mock_client

    with patch("app.llm.tool_handlers.run_job"):
        with patch("app.llm.tool_handlers.store.ingest_internal_event"):
            with patch("app.llm.tool_handlers.store.get_event") as mock_get_event:
                from app.models.decision import DecisionResult, ExecutionResult, ExecutionStatus, PolicyResult, RiskLevel
                from app.models.intent import Intent
                mock_result = MagicMock()
                mock_result.status.value = "completed"
                mock_result.status = MagicMock()
                mock_result.status.__eq__ = lambda self, other: other.value == "completed"

                from app.models.event import EventStatus
                evt = MagicMock()
                evt.status = EventStatus.COMPLETED
                pr = MagicMock()
                pr.estimated_spend = 80.0
                er = MagicMock()
                er.status = ExecutionStatus.EXECUTED
                er.sp_api_result = {"order_id": "MOCK-PO-ABC", "status": "success"}
                result = MagicMock()
                result.policy_result = pr
                result.execution_result = er
                evt.result = result
                mock_get_event.return_value = evt

                reply = agent_module.run_agent("reorder 10 units of WIDGET-42", S001)

    assert "done" in reply.lower() or "reorder" in reply.lower()
    assert mock_client.messages.create.call_count == 2


@patch("app.llm.agent._get_client")
def test_agent_dispatches_list_approvals(mock_get_client):
    tool_response = _make_tool_use_response("list_approvals", {})
    followup = _make_text_response("You have no pending approvals.")

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [tool_response, followup]
    mock_get_client.return_value = mock_client

    with patch("app.llm.tool_handlers.store.get_pending_approvals_for_seller", return_value=[]):
        reply = agent_module.run_agent("show my approvals", S001)

    assert "approvals" in reply.lower()
    assert mock_client.messages.create.call_count == 2


@patch("app.llm.agent._get_client")
def test_agent_dispatches_get_refund_rate(mock_get_client):
    tool_response = _make_tool_use_response("get_refund_rate", {})
    followup = _make_text_response("Your refund rate is 5.0%.")

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [tool_response, followup]
    mock_get_client.return_value = mock_client

    with patch("app.llm.tool_handlers.store.get_recent_events_by_type", return_value=[]):
        reply = agent_module.run_agent("what is my refund rate?", S001)

    assert "refund" in reply.lower()
    assert mock_client.messages.create.call_count == 2


@patch("app.llm.agent._get_client")
def test_agent_returns_text_when_no_tool_needed(mock_get_client):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_text_response(
        "I'm not sure how to help with that."
    )
    mock_get_client.return_value = mock_client

    reply = agent_module.run_agent("what is the meaning of life?", S001)

    assert "not sure" in reply.lower()
    assert mock_client.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# tool_handlers — reorder_sku (uses real DB)
# ---------------------------------------------------------------------------

def test_reorder_sku_low_risk_returns_executed_message():
    with TestClient(app):
        result = tool_handlers.reorder_sku(sku="WIDGET-42", quantity=10, seller=S001)
    # S001: 10 units * $8 = $80 — below auto-approve limits
    assert "MOCK-PO-" in result or "submitted" in result.lower()


def test_reorder_sku_high_risk_returns_escalated_message():
    with TestClient(app):
        result = tool_handlers.reorder_sku(sku="WIDGET-42", quantity=200, seller=S001)
    # S001: 200 units * $8 = $1600 — above auto_approve_max_spend ($500)
    assert "approval" in result.lower() or "escalat" in result.lower()


# ---------------------------------------------------------------------------
# tool_handlers — list_approvals (uses real DB)
# ---------------------------------------------------------------------------

def test_list_approvals_empty():
    with TestClient(app):
        result = tool_handlers.list_approvals(seller=S001)
    assert result == "No pending approvals."


def test_list_approvals_with_pending():
    with TestClient(app) as client:
        # Create a HIGH-risk event for S002 to generate a pending approval
        resp = client.post("/events", json={
            "seller_id": "S002",
            "event_type": "inventory_low",
            "payload": {"sku": "BULK-01", "current_quantity": 5},
        })
        client.get(f"/events/{resp.json()['event_id']}")  # wait for completion
        result = tool_handlers.list_approvals(seller=S002)
    assert "pending approval" in result.lower()
    assert "reorder_BULK-01" in result or "bulk-01" in result.lower() or "BULK-01" in result


# ---------------------------------------------------------------------------
# tool_handlers — get_refund_rate (uses real DB)
# ---------------------------------------------------------------------------

def test_get_refund_rate_no_data():
    with TestClient(app):
        result = tool_handlers.get_refund_rate(seller=S001)
    assert "no refund rate data" in result.lower()


def test_get_refund_rate_with_data():
    with TestClient(app) as client:
        client.post("/events", json={
            "seller_id": "S001",
            "event_type": "high_refund_rate_detected",
            "payload": {"refund_count": 5, "order_count": 100, "window_minutes": 1440},
        })
        result = tool_handlers.get_refund_rate(seller=S001)
    assert "5.0%" in result
    assert "5" in result and "100" in result


# ---------------------------------------------------------------------------
# policy engine — requested_quantity patch
# ---------------------------------------------------------------------------

def test_policy_uses_requested_quantity_from_payload():
    """Manual reorder quantity goes through the policy engine correctly."""
    from app.engine import policy as policy_engine
    from app.models.intent import Intent

    # S001 limit: 50 units / $500. Requesting 10 units → LOW risk
    result = policy_engine.evaluate(
        Intent.REORDER,
        S001,
        {"sku": "WIDGET-42", "requested_quantity": 10},
    )
    assert result.risk_level.value == "LOW"
    assert result.recommended_quantity == 10

    # Requesting 200 units → HIGH risk (above auto_approve_max_units=50)
    result = policy_engine.evaluate(
        Intent.REORDER,
        S001,
        {"sku": "WIDGET-42", "requested_quantity": 200},
    )
    assert result.risk_level.value == "HIGH"
    assert result.recommended_quantity == 200


def test_policy_falls_back_to_seller_default_when_no_requested_quantity():
    """Existing inventory_low events without requested_quantity still use policy default."""
    from app.engine import policy as policy_engine
    from app.models.intent import Intent

    result = policy_engine.evaluate(
        Intent.REORDER,
        S001,
        {"sku": "WIDGET-42", "current_quantity": 3},
    )
    # S001 default: reorder_quantity=40 → LOW risk (40 < 50 limit)
    assert result.recommended_quantity == 40
    assert result.risk_level.value == "LOW"
