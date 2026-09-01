from enum import Enum
from typing import Optional

from pydantic import BaseModel


class SellerStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class InventoryPolicy(BaseModel):
    reorder_point: int
    reorder_quantity: int
    auto_approve_max_units: int
    auto_approve_max_spend: float
    unit_cost: float


class OrderSpikePolicy(BaseModel):
    auto_approve_max_multiplier: float  # spike above this ratio → HIGH risk


class RefundRatePolicy(BaseModel):
    auto_approve_max_rate: float  # refund rate above this fraction → HIGH risk (e.g. 0.10 = 10%)


class SellerPolicies(BaseModel):
    inventory_low: InventoryPolicy
    order_spike: OrderSpikePolicy
    high_refund_rate: RefundRatePolicy


class SlackCredentials(BaseModel):
    bot_token: str


class Seller(BaseModel):
    id: str
    name: str
    status: SellerStatus
    policies: SellerPolicies
    slack_channel_id: Optional[str] = None  # notification channel for this seller
    slack_user_id: Optional[str] = None
    slack_credentials: Optional[SlackCredentials] = None
    # Platform credentials live on seller_platform_accounts — one row per
    # (platform, external_id) — not here. See app/models/platform.py.
