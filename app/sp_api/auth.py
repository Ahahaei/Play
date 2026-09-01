import os

import httpx

from app.models.platform import AmazonCredentials, Platform


def amazon_credentials(seller) -> AmazonCredentials:
    """Fetch the seller's Amazon credentials from their platform account."""
    from app import store

    account = store.get_account_for_seller(seller.id, Platform.AMAZON)
    if account is None or account.credentials is None:
        raise ValueError(f"Seller '{seller.id}' has no SP API credentials configured")
    return account.credentials


def get_access_token(seller) -> str:
    """Exchange LWA refresh_token for a short-lived access_token."""
    if os.environ.get("SP_API_ENABLED", "false").lower() != "true":
        return "mock_access_token"

    creds = amazon_credentials(seller)

    resp = httpx.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": creds.lwa_refresh_token,
            "client_id": creds.lwa_client_id,
            "client_secret": creds.lwa_client_secret,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]
