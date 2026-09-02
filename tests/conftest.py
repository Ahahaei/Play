import os
from unittest.mock import patch

# Must be set before any app imports trigger engine creation.
os.environ["SP_API_ENABLED"] = "false"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("INTERNAL_INGEST_SECRET", "test-internal-secret")

import pytest
from sqlalchemy import text

from app.db.engine import SessionLocal, create_tables
from app.db.seed import seed_sellers
from app.routers.deps import require_internal_token
from main import app


@pytest.fixture(scope="session", autouse=True)
def setup_database():
    create_tables()
    seed_sellers()


@pytest.fixture(autouse=True)
def mock_slack_client():
    """Prevent tests from making real Slack API calls."""
    with patch("app.slack.client.send_approval_request", return_value="1234567890.000001"), \
         patch("app.slack.client.update_message"), \
         patch("app.slack.client.send_message"):
        yield


@pytest.fixture(autouse=True)
def bypass_internal_auth():
    """Let the existing suite post to the internal endpoints without a header.

    The guard itself is exercised in tests/test_internal_auth.py, which drops
    this override.
    """
    app.dependency_overrides[require_internal_token] = lambda: None
    yield
    app.dependency_overrides.pop(require_internal_token, None)


@pytest.fixture(autouse=True)
def clear_tables():
    yield
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM approvals"))
        db.execute(text("DELETE FROM jobs"))
        db.execute(text("DELETE FROM events"))
        db.commit()
    finally:
        db.close()
