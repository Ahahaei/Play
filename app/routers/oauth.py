import base64
import json
import os
import urllib.parse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from app import store
from app.models.platform import AmazonCredentials, Platform

router = APIRouter(prefix="/oauth", tags=["oauth"])

_LWA_AUTHORIZE_URL = "https://www.amazon.com/ap/oa"
_LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"


@router.get("/authorize")
def authorize(
    seller_id: str = Query(...),
    marketplace_id: str = Query(default="ATVPDKIKX0DER"),  # North America default
    endpoint: str = Query(default="https://sellingpartnerapi-na.amazon.com"),
):
    seller = store.get_seller(seller_id)
    if seller is None:
        raise HTTPException(status_code=404, detail=f"Seller '{seller_id}' not found")

    client_id = os.environ.get("LWA_CLIENT_ID", "")
    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", "")
    if not client_id or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="LWA_CLIENT_ID or OAUTH_REDIRECT_URI not configured",
        )

    # Encode seller context in state so it round-trips through Amazon unchanged
    state_data = {
        "seller_id": seller_id,
        "marketplace_id": marketplace_id,
        "endpoint": endpoint,
    }
    state = base64.urlsafe_b64encode(json.dumps(state_data).encode()).decode()

    params = {
        "client_id": client_id,
        "scope": "sellingpartnerapi::migration",
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    url = f"{_LWA_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=url)


@router.get("/callback")
def callback(
    state: str = Query(...),
    code: str = Query(None),
    spapi_oauth_code: str = Query(None),
    selling_partner_id: str = Query(
        ...,
        description="Amazon's merchant token for the authorizing seller — the "
                    "id inbound notifications carry as SellerId.",
    ),
    error: str = Query(None),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Amazon authorization failed: {error}")
    code = spapi_oauth_code or code
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        state_data = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        seller_id = state_data["seller_id"]
        marketplace_id = state_data["marketplace_id"]
        endpoint = state_data["endpoint"]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    seller = store.get_seller(seller_id)
    if seller is None:
        raise HTTPException(status_code=404, detail=f"Seller '{seller_id}' not found")

    client_id = os.environ.get("LWA_CLIENT_ID", "")
    client_secret = os.environ.get("LWA_CLIENT_SECRET", "")
    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", "")

    resp = httpx.post(
        _LWA_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {resp.text}")

    refresh_token = resp.json().get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=502, detail="No refresh_token in Amazon response")

    credentials = AmazonCredentials(
        lwa_client_id=client_id,
        lwa_client_secret=client_secret,
        lwa_refresh_token=refresh_token,
        marketplace_id=marketplace_id,
        endpoint=endpoint,
    )
    # Keyed on the merchant token, not on our seller_id: that is what inbound
    # pushes carry, so this is the row that resolves them back to a seller.
    store.upsert_account(
        platform=Platform.AMAZON,
        external_id=selling_partner_id,
        seller_id=seller_id,
        credentials=credentials,
    )

    return {"ok": True, "seller_id": seller_id, "message": "Amazon account connected successfully"}
