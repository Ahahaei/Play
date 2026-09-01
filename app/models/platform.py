from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field


class Platform(str, Enum):
    AMAZON = "amazon"
    SHOPEE = "shopee"


class AmazonCredentials(BaseModel):
    """LWA bearer-token auth. The refresh token is long-lived and per seller;
    the client id/secret are per application (rotated every 180 days, which
    does not invalidate refresh tokens)."""

    platform: Literal["amazon"] = "amazon"
    lwa_client_id: str
    lwa_client_secret: str
    lwa_refresh_token: str
    marketplace_id: str
    endpoint: str  # e.g. "https://sandbox.sellingpartnerapi-fe.amazon.com"


class ShopeeCredentials(BaseModel):
    """HMAC-SHA256 request signing. `partner_id`/`partner_key` are per
    application — the key also verifies inbound push signatures — while the
    tokens are per shop: access valid 4 hours, refresh valid 30 days."""

    platform: Literal["shopee"] = "shopee"
    partner_id: str
    partner_key: str
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    access_token_expires_at: Optional[datetime] = None
    refresh_token_expires_at: Optional[datetime] = None
    region: str = "VN"


PlatformCredentials = Annotated[
    Union[AmazonCredentials, ShopeeCredentials],
    Field(discriminator="platform"),
]


class PlatformAccount(BaseModel):
    """One seller's account on one platform.

    `external_id` is the platform-native seller identifier that inbound pushes
    actually carry — Amazon's `SellerId` merchant token, Shopee's integer
    `shop_id`. Resolving it to our internal `seller_id` is the adapter's first
    job; a push for an unknown account is dead-lettered, never guessed at.
    """

    id: str
    platform: Platform
    external_id: str
    seller_id: str
    credentials: Optional[PlatformCredentials] = None
    created_at: datetime
