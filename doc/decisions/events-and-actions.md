# Decision: event taxonomy, triggers, and the write

**Decided:** 2026-09-01 · **Status:** agreed, not yet implemented

## The governing principle

**Risk attaches to the action, not to the observation.** An event is only worth raising if it implies
an action, and the risk level comes from the *magnitude of that action* — units and spend — never from
how dramatic the event looked. A 5× order spike on a SKU with a year of stock implies nothing and
raises nothing.

This is why `_evaluate_order_spike` (which scored a ratio and produced no quantity) is deleted, and
why every L2 signal now carries a `requested_quantity`.

## Event types

| Layer | Type | Origin |
|---|---|---|
| L1 domain | `order_created`, `order_paid`, `order_shipped`, `order_canceled` | platform push, normalized |
| L2 monitoring | `inventory_low` | derived: stock below a unit threshold |
| L2 monitoring | `order_spike_detected` | derived: velocity jump that threatens cover |
| L2 monitoring | `high_refund_rate_detected` | **no producer — open, see below** |

L1 events are recorded and never decided on. L2 events run the full decision pipeline. Detectors run
**only** on the L1 branch, so an L2 signal can never beget another — the guard is structural.

## The two stock signals

They are not variants of each other. They answer different questions.

| | `inventory_low` | `order_spike_detected` |
|---|---|---|
| Purpose | steady state — keep the shelf full | isolated incident — a special occasion |
| Trigger | `stock < reorder_point` (units) | velocity jump, per SKU |
| X (top-up) | `reorder_quantity`, **configured**; defaults to `2 × reorder_point` for new sellers | **calculated** (below) |
| Typical risk | almost always LOW once the seller has tuned it — that is the loop working | scales with the spike; escalates when large |
| Scope now | unchanged from today | **this is what we build** |

### `order_spike_detected` — the calculation

```
detect (suddenness):  units this window_minutes bucket
                      vs mean of the previous 3 complete buckets
                      fire when ratio >= detect_multiplier
                      floors: units >= min_orders, 3 buckets of history

rate for X:           units of this SKU over the trailing 24h, per day
days_to_sellout:      stock / velocity
X:                    ceil(velocity × spike_cover_days − stock)
                      X <= 0 → well stocked → emit nothing
```

**Detection uses the short window; X uses the 24-hour rate.** Extrapolating a 60-minute burst to a
daily rate produces order quantities an order of magnitude too large (25 units/hour → 600/day →
4,140 units). The short window answers "is this sudden?"; only the 24h rate may answer "how much do I
buy?" If the surge is real, the next day's rate has caught up and buys more.

`days_to_sellout` is the seller-legible number and replaces `baseline_count` in all messaging.

## Identity and deduplication

- Inbound pushes carry a platform-native id (`SellerId`, `shop_id`), never our `seller_id`.
  `seller_platform_accounts` resolves `(platform, external_id) → seller_id`. Unknown account =
  dead-letter, never a guess.
- **The dedup key is adapter-supplied**, because it differs in kind: Amazon `NotificationMetadata.NotificationId`;
  Shopee has none, so `sha256(shop_id|code|ordersn|status|update_time)`.
- Derived events synthesize their own and **must include `seller_id`**, since the unique constraint is
  `(platform, dedup_key)` and is not seller-scoped:
  - `derived:{seller_id}:order_spike:{sku}:{date}` — at most one spike top-up per SKU per day.
- `platform` must be **non-NULL** on any event that needs dedup; NULLs do not collide, so a NULL
  silently disables the guard. Derived events inherit the source platform, falling back to `"internal"`.
- `normalize()` returns a **list**: one Shopee `code=3` push fans out to up to four internal events via
  `data.status`; some transitions map to nothing.

## Intents and the write

> **Deferred, not undecided.** Everything in this section is the agreed contract; none of it is built.
> The boundary Protocol is inbound-only and the pipeline stops at the decision — see P12 in
> [pipeline-and-scale.md](pipeline-and-scale.md). The legacy mock write in
> `app/platforms/amazon/client.py` still runs and still returns `MOCK-PO-…`; it targets Amazon's
> Vendor (1P) program, which is the wrong one, and it is replaced rather than adapted when the
> outbound half lands.


- `Intent.REORDER` → **`SET_STOCK`**. It is a set-absolute stock write, not procurement.
- `FLAG_ORDER_SPIKE` is removed. Both `inventory_low` and `order_spike_detected` classify to
  `SET_STOCK` and share one policy rule (`_evaluate_set_stock`); they differ only in how X was derived
  and how the message reads.
- **Delta in, absolute out.** Seller input and detector output are deltas; every platform write is
  absolute. The adapter closes the gap by reading current stock, and the computed target is persisted
  on the job row *before* the write so a retry rewrites the same value.
- Risk gate: X vs `auto_approve_max_units`, `X × unit_cost` vs `auto_approve_max_spend` — the
  `InventoryPolicy` limits, shared by both producers. A large X escalating to a human is also the
  safety net against a bad velocity estimate.
- `supports_stock_write` is an adapter capability flag. Where no write exists (Amazon FBA, Shopee
  without `update_stock`): an automated L2 signal **notifies** ("ship units in"); a manual chat request
  does **nothing** — the seller already knows.
- `ExecutionStatus.NOTIFIED` is the third outcome alongside EXECUTED and ESCALATED.
- `WriteResult { status: applied | accepted | failed, per_item[], trace_id }`. `accepted` is a real
  state (async writes), not a synonym for success.
- `classify_error → retryable | terminal | ambiguous` is **adapter-owned**: Shopee returns failures as
  HTTP 200 with a populated `error` field, so status codes are not a generic retryability signal.
  `ambiguous` is safe to retry here only because the target is persisted and the write is set-absolute.

## Variables

**Policy — `InventoryPolicy`** (unchanged): `reorder_point` (units), `reorder_quantity` (X, default
`2 × reorder_point`), `auto_approve_max_units`, `auto_approve_max_spend`, `unit_cost`.

**Policy — `OrderSpikePolicy`** (replaced): `detect_multiplier` (1.5), `window_minutes` (60),
`min_orders` (5), `spike_cover_days` (7). `auto_approve_max_multiplier` is **removed** — it was a risk
gate and risk no longer comes from the ratio. All defaulted; `policies` is JSON, so no migration.

**`sku_stock(account_id, sku)`** — `quantity`, `source` (poll|derived|write), `polled_at`, `updated_at`.
Keyed on the account, not the seller: Amazon stock and Shopee stock are different numbers for one SKU.
Decremented by `order_created`, absolute-set by `get_stock`/`set_stock`, lazily filled by one
`get_stock` per SKU per 6h TTL.

> **The cache gates detection; a live read gates the write.** Drift (cancellations, returns, manual
> Seller Central edits) can therefore only cause a spurious or missed *signal*, never a wrong quantity
> written to a platform.

**`order_spike_detected` payload:** `sku`, `current_quantity`, `velocity_per_day`,
`baseline_velocity`, `days_to_sellout`, `spike_cover_days`, `requested_quantity`, `window_minutes`.

**`inventory_low` payload:** `sku`, `current_quantity`, `requested_quantity`.

## Open

- **`high_refund_rate_detected` has no producer and no action to score.** Under "risk is about the
  action" it is a pure notification, which does not fit this frame. Needs a separate decision: does it
  stay an event, or become a report? Feeding it would also require an `order_refunded` L1 type that no
  adapter emits.
- **Demand collapse is not detected.** A drop from 10/window to 2 is as interesting as a spike, and
  nothing looks for it. No event type exists; adding one is a deliberate future choice.
- A well-stocked spike notifies nobody and is not recorded. Accepted for now.
