"""The inbound half of the platform boundary.

`verify` and `normalize` only. The outbound half — `get_stock`, `set_stock`,
`classify_error`, the stock-write capability flag — is deliberately not part of
this Protocol yet; the pipeline stops at the decision. See
`doc/decisions/pipeline-and-scale.md`.

Everything here must be pure: no credentials fetched, no network, no database.
Both stages are terminal on failure — a bad signature never becomes good, and a
payload that will not parse will not parse on the retry either.
"""
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from app.models.event import EventType


class PlatformError(Exception):
    """Base for boundary failures."""


class UnknownPlatform(PlatformError):
    """No adapter is registered under that name."""


class NormalizationError(PlatformError):
    """The delivery is structurally unusable. Terminal — send it to poison."""


@dataclass(frozen=True)
class RawDelivery:
    """One inbound delivery, transport-agnostic.

    Today every delivery arrives over HTTP. `attributes` exists so an SQS
    message (Amazon's real transport) can be carried later without changing the
    Protocol.
    """

    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, str] = field(default_factory=dict)
    attributes: Mapping[str, Any] = field(default_factory=dict)
    url: str = ""  # full callback URL — part of Shopee's HMAC base string

    def header(self, name: str) -> Optional[str]:
        """Case-insensitive header lookup."""
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None


@dataclass(frozen=True)
class NormalizedEvent:
    """One internal event derived from a delivery.

    `external_id` is the platform-native seller id — the core resolves it to a
    `seller_id`; the adapter never learns ours. `raw_payload` is kept as
    evidence, not as the record of truth.
    """

    external_id: str
    event_type: EventType
    payload: dict
    dedup_key: str
    raw_payload: dict


@runtime_checkable
class InboundAdapter(Protocol):
    name: str

    def verify(self, raw: RawDelivery) -> bool:
        """Authenticate the delivery. False is terminal, never retried."""
        ...

    def normalize(self, raw: RawDelivery) -> list[NormalizedEvent]:
        """Zero or more internal events.

        Zero-or-many is the honest signature: one platform status maps to
        nothing we track as often as it maps to something, and one push code
        covers several internal event types.
        """
        ...
