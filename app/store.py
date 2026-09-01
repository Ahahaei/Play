import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.db.engine import SessionLocal
from app.db.models import ApprovalRow, EventRow, SellerPlatformAccountRow, SellerRow
from app.models.approval import ApprovalStatus, PendingApproval
from app.models.decision import DecisionResult
from app.models.event import EventRecord, EventStatus, EventType
from app.models.platform import Platform, PlatformAccount
from app.models.seller import Seller


@contextmanager
def _session():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _seller_from_row(row: SellerRow) -> Seller:
    return Seller.model_validate({
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "slack_channel_id": row.slack_channel_id,
        "slack_user_id": row.slack_user_id,
        "policies": row.policies,
        "slack_credentials": row.slack_credentials,
    })


def _account_from_row(row: SellerPlatformAccountRow) -> PlatformAccount:
    return PlatformAccount.model_validate({
        "id": row.id,
        "platform": row.platform,
        "external_id": row.external_id,
        "seller_id": row.seller_id,
        "credentials": row.credentials,
        "created_at": _ensure_utc(row.created_at),
    })


def _event_from_row(row: EventRow) -> EventRecord:
    return EventRecord.model_validate({
        "id": row.id,
        "seller_id": row.seller_id,
        "event_type": row.event_type,
        "payload": row.payload,
        "status": row.status,
        "result": row.result,
        "error": row.error,
        "created_at": _ensure_utc(row.created_at),
        "updated_at": _ensure_utc(row.updated_at),
    })


def _approval_from_row(row: ApprovalRow) -> PendingApproval:
    return PendingApproval.model_validate({
        "id": row.id,
        "event_id": row.event_id,
        "seller_id": row.seller_id,
        "intent": row.intent,
        "policy_result": row.policy_result,
        "status": row.status,
        "created_at": _ensure_utc(row.created_at),
        "resolved_at": _ensure_utc(row.resolved_at),
        "resolved_by": row.resolved_by,
        "slack_channel_id": row.slack_channel_id,
        "slack_ts": row.slack_ts,
    })


# --- Seller ---

def get_seller(seller_id: str) -> Optional[Seller]:
    with _session() as db:
        row = db.get(SellerRow, seller_id)
        return _seller_from_row(row) if row else None


def get_seller_by_slack_user_id(slack_user_id: str) -> Optional[Seller]:
    with _session() as db:
        row = db.execute(
            select(SellerRow).where(SellerRow.slack_user_id == slack_user_id)
        ).scalar_one_or_none()
        return _seller_from_row(row) if row else None


def create_seller(seller: Seller) -> None:
    with _session() as db:
        db.add(SellerRow(
            id=seller.id,
            name=seller.name,
            status=seller.status.value,
            slack_channel_id=seller.slack_channel_id,
            slack_user_id=seller.slack_user_id,
            policies=seller.policies.model_dump(mode="json"),
            slack_credentials=(
                seller.slack_credentials.model_dump(mode="json")
                if seller.slack_credentials else None
            ),
        ))


def update_seller(seller_id: str, updates: dict) -> Optional[Seller]:
    with _session() as db:
        row = db.get(SellerRow, seller_id)
        if row is None:
            return None
        for field, value in updates.items():
            setattr(row, field, value)
        db.flush()
        return _seller_from_row(row)


def list_sellers() -> list[Seller]:
    with _session() as db:
        rows = db.execute(select(SellerRow)).scalars().all()
        return [_seller_from_row(row) for row in rows]


# --- Platform accounts ---

def get_account(platform: Platform, external_id: str) -> Optional[PlatformAccount]:
    """Resolve a platform-native seller id to the account that owns it.

    This is the identity resolution every inbound push depends on: a delivery
    carries `SellerId` or `shop_id`, never our internal seller_id.
    """
    with _session() as db:
        row = db.execute(
            select(SellerPlatformAccountRow)
            .where(SellerPlatformAccountRow.platform == Platform(platform).value)
            .where(SellerPlatformAccountRow.external_id == external_id)
        ).scalar_one_or_none()
        return _account_from_row(row) if row else None


def get_account_for_seller(seller_id: str, platform: Platform) -> Optional[PlatformAccount]:
    with _session() as db:
        row = db.execute(
            select(SellerPlatformAccountRow)
            .where(SellerPlatformAccountRow.seller_id == seller_id)
            .where(SellerPlatformAccountRow.platform == Platform(platform).value)
        ).scalars().first()
        return _account_from_row(row) if row else None


def list_accounts_for_seller(seller_id: str) -> list[PlatformAccount]:
    with _session() as db:
        rows = db.execute(
            select(SellerPlatformAccountRow)
            .where(SellerPlatformAccountRow.seller_id == seller_id)
            .order_by(SellerPlatformAccountRow.platform)
        ).scalars().all()
        return [_account_from_row(row) for row in rows]


def upsert_account(
    platform: Platform,
    external_id: str,
    seller_id: str,
    credentials=None,
) -> PlatformAccount:
    """Create or update the account for `(platform, external_id)`.

    Re-authorization is an update, not a second account — the unique constraint
    is on the platform's own identifier, so the same shop reconnecting keeps
    one row and gets fresh credentials.
    """
    platform_value = Platform(platform).value
    payload = credentials.model_dump(mode="json") if credentials is not None else None
    with _session() as db:
        row = db.execute(
            select(SellerPlatformAccountRow)
            .where(SellerPlatformAccountRow.platform == platform_value)
            .where(SellerPlatformAccountRow.external_id == external_id)
        ).scalar_one_or_none()
        if row is None:
            row = SellerPlatformAccountRow(
                id=str(uuid.uuid4()),
                platform=platform_value,
                external_id=external_id,
                seller_id=seller_id,
                credentials=payload,
                created_at=datetime.now(timezone.utc),
            )
            db.add(row)
        else:
            row.seller_id = seller_id
            if payload is not None:
                row.credentials = payload
        db.flush()
        return _account_from_row(row)


# --- Events ---

def create_event(record: EventRecord) -> None:
    with _session() as db:
        db.add(EventRow(
            id=record.id,
            seller_id=record.seller_id,
            event_type=record.event_type.value,
            payload=record.payload,
            status=record.status.value,
            result=None,
            error=None,
            created_at=record.created_at,
            updated_at=record.updated_at,
        ))


def get_event(event_id: str) -> Optional[EventRecord]:
    with _session() as db:
        row = db.get(EventRow, event_id)
        return _event_from_row(row) if row else None


def set_event_processing(event_id: str) -> None:
    with _session() as db:
        row = db.get(EventRow, event_id)
        row.status = EventStatus.PROCESSING.value
        row.updated_at = datetime.now(timezone.utc)


def set_event_completed(event_id: str, result: Optional[DecisionResult]) -> None:
    with _session() as db:
        row = db.get(EventRow, event_id)
        row.status = EventStatus.COMPLETED.value
        row.result = result.model_dump(mode="json") if result else None
        row.updated_at = datetime.now(timezone.utc)


def set_event_failed(event_id: str, error: str) -> None:
    with _session() as db:
        row = db.get(EventRow, event_id)
        row.status = EventStatus.FAILED.value
        row.error = error
        row.updated_at = datetime.now(timezone.utc)


# --- Approvals ---

def create_approval(record: PendingApproval) -> None:
    with _session() as db:
        db.add(ApprovalRow(
            id=record.id,
            event_id=record.event_id,
            seller_id=record.seller_id,
            intent=record.intent.value,
            policy_result=record.policy_result.model_dump(mode="json"),
            status=record.status.value,
            created_at=record.created_at,
            slack_channel_id=record.slack_channel_id,
        ))


def get_approval(approval_id: str) -> Optional[PendingApproval]:
    with _session() as db:
        row = db.get(ApprovalRow, approval_id)
        return _approval_from_row(row) if row else None


def resolve_approval(approval_id: str, status: ApprovalStatus, resolved_by: str) -> None:
    with _session() as db:
        row = db.get(ApprovalRow, approval_id)
        row.status = status.value
        row.resolved_at = datetime.now(timezone.utc)
        row.resolved_by = resolved_by


def set_approval_slack_ts(approval_id: str, ts: str) -> None:
    with _session() as db:
        row = db.get(ApprovalRow, approval_id)
        row.slack_ts = ts


def get_pending_approvals_for_seller(seller_id: str) -> list[PendingApproval]:
    with _session() as db:
        rows = db.execute(
            select(ApprovalRow)
            .where(ApprovalRow.seller_id == seller_id)
            .where(ApprovalRow.status == ApprovalStatus.PENDING.value)
            .order_by(ApprovalRow.created_at.desc())
        ).scalars().all()
        return [_approval_from_row(row) for row in rows]


def get_recent_events_by_type(
    seller_id: str, event_type: EventType, limit: int = 1
) -> list[EventRecord]:
    with _session() as db:
        rows = db.execute(
            select(EventRow)
            .where(EventRow.seller_id == seller_id)
            .where(EventRow.event_type == event_type.value)
            .where(EventRow.status == EventStatus.COMPLETED.value)
            .order_by(EventRow.created_at.desc())
            .limit(limit)
        ).scalars().all()
        return [_event_from_row(row) for row in rows]


def set_event_sp_api_result(event_id: str, sp_result: dict) -> None:
    with _session() as db:
        row = db.get(EventRow, event_id)
        if row and row.result:
            execution_result = {**row.result["execution_result"], "sp_api_result": sp_result}
            row.result = {**row.result, "execution_result": execution_result}
            row.updated_at = datetime.now(timezone.utc)
