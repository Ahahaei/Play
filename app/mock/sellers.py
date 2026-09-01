from app.models.platform import AmazonCredentials, Platform
from app.models.seller import (
    InventoryPolicy,
    OrderSpikePolicy,
    RefundRatePolicy,
    Seller,
    SellerPolicies,
    SellerStatus,
    SlackCredentials,
)

_PLACEHOLDER_CREDENTIALS = AmazonCredentials(
    lwa_client_id="amzn1.application-oa2-client.PLACEHOLDER",
    lwa_client_secret="PLACEHOLDER_SECRET",
    lwa_refresh_token="Atzr|PLACEHOLDER_REFRESH_TOKEN",
    marketplace_id="A15PK738MTQHUU",  # Vietnam marketplace
    endpoint="https://sandbox.sellingpartnerapi-fe.amazon.com",
)

MOCK_SELLERS: list[Seller] = [
    Seller(
        id="S001",
        name="Gadget Galaxy",
        status=SellerStatus.ACTIVE,
        slack_channel_id="C0AJBKT8U1L",
        slack_user_id="U_MOCK_S001",
        slack_credentials=SlackCredentials(bot_token="xoxb-mock-s001-token"),
        policies=SellerPolicies(
            inventory_low=InventoryPolicy(
                reorder_point=5,
                reorder_quantity=40,
                auto_approve_max_units=50,
                auto_approve_max_spend=500.0,
                unit_cost=8.00,
            ),
            # spike up to 2x baseline → LOW; above 2x → HIGH
            order_spike=OrderSpikePolicy(auto_approve_max_multiplier=2.0),
            # refund rate up to 10% → LOW; above 10% → HIGH
            high_refund_rate=RefundRatePolicy(auto_approve_max_rate=0.10),
        ),
    ),
    # reorder 40 * $8.00 = $320 < $500, 40 < 50 → LOW risk by default
    Seller(
        id="S002",
        name="Bulk Barn",
        status=SellerStatus.ACTIVE,
        slack_channel_id="C0AJBKT8U1L",
        slack_user_id="U_MOCK_S002",
        slack_credentials=SlackCredentials(bot_token="xoxb-mock-s002-token"),
        policies=SellerPolicies(
            inventory_low=InventoryPolicy(
                reorder_point=10,
                reorder_quantity=200,
                auto_approve_max_units=100,
                auto_approve_max_spend=800.0,
                unit_cost=5.00,
            ),
            # tighter threshold — spike above 1.5x baseline → HIGH
            order_spike=OrderSpikePolicy(auto_approve_max_multiplier=1.5),
            # tighter threshold — refund rate above 5% → HIGH
            high_refund_rate=RefundRatePolicy(auto_approve_max_rate=0.05),
        ),
    ),
    # reorder 200 * $5.00 = $1000 > $800, 200 > 100 → HIGH risk by default
    Seller(
        id="S003",
        name="Dormant Shop",
        status=SellerStatus.INACTIVE,
        slack_channel_id="C_PLACEHOLDER_S003",
        slack_user_id="U_MOCK_S003",
        slack_credentials=SlackCredentials(bot_token="xoxb-mock-s003-token"),
        policies=SellerPolicies(
            inventory_low=InventoryPolicy(
                reorder_point=5,
                reorder_quantity=20,
                auto_approve_max_units=50,
                auto_approve_max_spend=500.0,
                unit_cost=10.00,
            ),
            order_spike=OrderSpikePolicy(auto_approve_max_multiplier=2.0),
            high_refund_rate=RefundRatePolicy(auto_approve_max_rate=0.10),
        ),
    ),
]

# Platform-native seller ids. Amazon's real ones are merchant tokens like
# "ABCDEFGFMDKELDW"; these are deliberately recognisable as mocks.
MOCK_PLATFORM_ACCOUNTS: list[dict] = [
    {
        "platform": Platform.AMAZON,
        "external_id": "A_MOCK_S001",
        "seller_id": "S001",
        "credentials": _PLACEHOLDER_CREDENTIALS,
    },
    {
        "platform": Platform.AMAZON,
        "external_id": "A_MOCK_S002",
        "seller_id": "S002",
        "credentials": _PLACEHOLDER_CREDENTIALS,
    },
    {
        "platform": Platform.AMAZON,
        "external_id": "A_MOCK_S003",
        "seller_id": "S003",
        "credentials": _PLACEHOLDER_CREDENTIALS,
    },
]
