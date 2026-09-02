"""Platform name → inbound adapter.

The ingest route is generic (`/webhooks/{platform}`); this is the only place
that knows which adapters exist.
"""
from app.models.platform import Platform
from app.platforms.amazon.adapter import AmazonInboundAdapter
from app.platforms.base import InboundAdapter, UnknownPlatform
from app.platforms.shopee.adapter import ShopeeInboundAdapter

_ADAPTERS: dict[str, InboundAdapter] = {}


def register(adapter: InboundAdapter) -> None:
    _ADAPTERS[adapter.name] = adapter


def get(name: str) -> InboundAdapter:
    """Resolve an adapter, or raise `UnknownPlatform` (a 404, not a retry)."""
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise UnknownPlatform(f"No adapter registered for platform '{name}'") from None


def names() -> list[str]:
    return sorted(_ADAPTERS)


register(AmazonInboundAdapter())
register(ShopeeInboundAdapter())

# Every Platform enum member must have an adapter, or ingest 404s on a platform
# the rest of the system believes in. Not an assert — this must hold under -O.
_missing = {p.value for p in Platform} - set(_ADAPTERS)
if _missing:
    raise RuntimeError(f"No inbound adapter registered for platform(s): {sorted(_missing)}")
