import hmac
import os

from fastapi import Header, HTTPException


def require_internal_token(x_internal_token: str = Header(default="")) -> None:
    """Guard the internal ingest endpoints.

    `/events` and `/webhooks/sp-api` accept a caller-supplied `seller_id` and
    reach the auto-execute path, so they must never be open to the internet.
    Fails closed: an unset `INTERNAL_INGEST_SECRET` rejects everything rather
    than admitting everything.
    """
    expected = os.environ.get("INTERNAL_INGEST_SECRET", "")
    if not expected or not hmac.compare_digest(expected, x_internal_token or ""):
        raise HTTPException(status_code=401, detail="Invalid or missing internal token")
