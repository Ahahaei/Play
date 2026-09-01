"""Phase A / A1 — platform account identity.

An inbound push carries a platform-native seller id (Amazon's SellerId,
Shopee's shop_id), never our internal seller_id. These tests cover the
resolution in both directions and the credential storage that moved off
`sellers` onto the account table.
"""

import pytest

from app import store
from app.models.platform import AmazonCredentials, Platform, ShopeeCredentials


def test_resolve_external_id_to_seller():
    account = store.get_account(Platform.AMAZON, "A_MOCK_S001")
    assert account is not None
    assert account.seller_id == "S001"
    assert account.platform == Platform.AMAZON


def test_unknown_external_id_returns_none():
    # A push for an unknown account is dead-lettered, never guessed at.
    assert store.get_account(Platform.AMAZON, "A_NOT_CONNECTED") is None


def test_external_id_is_scoped_to_platform():
    # The same string on another platform is a different account.
    assert store.get_account(Platform.SHOPEE, "A_MOCK_S001") is None


def test_reverse_lookup_from_seller():
    account = store.get_account_for_seller("S002", Platform.AMAZON)
    assert account is not None
    assert account.external_id == "A_MOCK_S002"


def test_credentials_round_trip_as_amazon_model():
    account = store.get_account(Platform.AMAZON, "A_MOCK_S001")
    assert isinstance(account.credentials, AmazonCredentials)
    assert account.credentials.lwa_refresh_token == "Atzr|PLACEHOLDER_REFRESH_TOKEN"
    assert account.credentials.marketplace_id == "A15PK738MTQHUU"


def test_shopee_credentials_round_trip():
    # The Shopee shape does not fit the Amazon-shaped column that used to
    # live on `sellers` — partner key per app, tokens per shop.
    store.upsert_account(
        platform=Platform.SHOPEE,
        external_id="30011",
        seller_id="S001",
        credentials=ShopeeCredentials(
            partner_id="1000000",
            partner_key="test-partner-key",
            access_token="shop-access-token",
            refresh_token="shop-refresh-token",
        ),
    )
    account = store.get_account(Platform.SHOPEE, "30011")
    assert isinstance(account.credentials, ShopeeCredentials)
    assert account.credentials.partner_key == "test-partner-key"
    assert account.seller_id == "S001"


def test_reauthorization_updates_the_same_account():
    first = store.upsert_account(
        platform=Platform.SHOPEE,
        external_id="40022",
        seller_id="S001",
        credentials=ShopeeCredentials(partner_id="1", partner_key="old-key"),
    )
    second = store.upsert_account(
        platform=Platform.SHOPEE,
        external_id="40022",
        seller_id="S001",
        credentials=ShopeeCredentials(partner_id="1", partner_key="new-key"),
    )
    assert second.id == first.id
    assert store.get_account(Platform.SHOPEE, "40022").credentials.partner_key == "new-key"


def test_seller_no_longer_carries_platform_credentials():
    seller = store.get_seller("S001")
    assert seller is not None
    assert not hasattr(seller, "sp_api_credentials")


def test_seller_can_hold_accounts_on_several_platforms():
    store.upsert_account(
        platform=Platform.SHOPEE,
        external_id="50033",
        seller_id="S003",
        credentials=ShopeeCredentials(partner_id="1", partner_key="k"),
    )
    platforms = {a.platform for a in store.list_accounts_for_seller("S003")}
    assert platforms == {Platform.AMAZON, Platform.SHOPEE}


@pytest.fixture(autouse=True)
def clear_added_accounts():
    """Keep the seeded Amazon accounts, drop anything a test added."""
    yield
    from sqlalchemy import delete

    from app.db.engine import SessionLocal
    from app.db.models import SellerPlatformAccountRow

    db = SessionLocal()
    try:
        db.execute(
            delete(SellerPlatformAccountRow)
            .where(SellerPlatformAccountRow.platform != Platform.AMAZON.value)
        )
        db.commit()
    finally:
        db.close()
