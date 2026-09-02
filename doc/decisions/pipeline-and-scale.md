# Decision: reduced pipeline shape and scale posture

**Decided:** 2026-09-01 (revised same day — P3, P12) · **Status:** agreed, partially implemented
(migrations-as-release-step, identity schema, inbound adapter boundary landed)

Three pipeline shapes were drawn (VER 1, VER 2, REDUCED — see the end of [app/FIX.MD](../../app/FIX.MD)).
**We build REDUCED.** VER 2 is the target to grow into, not the thing to build now.

## The shape

```
POST /webhooks/{platform}          raw body, unmodified
      ▼
registry.get(platform) → adapter.verify → adapter.normalize → EventInput[]
      ▼
resolve (platform, external_id) → seller_id        unknown account = dead-letter
      ▼
┌── ONE transaction ─────────────────────────────────────┐
│ INSERT event ON CONFLICT (platform, dedup_key)         │
│              DO NOTHING RETURNING id                   │
│ INSERT job   — only if RETURNING gave an id            │
└────────────────────────────────────────────────────────┘
      ▼ commit → 202.  API process is done, nothing left in memory.

  ═════════ durability boundary ═════════

worker process, loop:
  claim job   (status='pending' AND run_after<=now)
           OR (status='processing' AND locked_at < now-5min)   ← reaper, folded in
           FOR UPDATE SKIP LOCKED, attempts += 1
      ▼
  run_pipeline
    L1 → record (+ run detectors) → done
    L2 → classify → policy → executor
         HIGH → approval → Slack → approved ─┐
         LOW ─────────────────────────────────┤
                                              ▼
                                    ╌╌ DEFERRED (P12) ╌╌
                        adapter.supports_stock_write?
                          no  → L2: notify · chat: nothing
                          yes → target = job.target_quantity
                                      ?? get_stock() + delta,
                                   SAVED TO THE JOB ROW before the write
                                → adapter.set_stock(target)
      ▼
  success → done · retryable/ambiguous → backoff, pending · terminal → dead
```

A seller DM (`reorder_sku`) is a second entry point that performs the **same INSERT pair**.

## Decisions

| # | Decision | Why |
|---|---|---|
| P1 | One HTTP receiver, `POST /webhooks/{platform}`, for every platform | Reduced scope. The SQS consumer Amazon actually requires is deferred. |
| P2 | Amazon inbound over HTTP is a **stand-in**, not production ingest | Amazon delivers to SQS/EventBridge and we poll; the adapter parses the real `ORDER_CHANGE` envelope so the consumer is later just a new `RawDelivery` source. |
| P3 | Shopee ships **inbound now**; its outbound half is deferred with every other write (P12) | A second real adapter is what forces the boundary to be platform-neutral, and the inbound half alone does that — Shopee's HMAC/`code=3`/synthesized-key model shares nothing with Amazon's. |
| P4 | `/webhooks/sp-api` and `/events` survive as **internal** endpoints behind a shared secret | They are unauthenticated today and `/events` reaches the auto-execute path. |
| P5 | Ack (202 / SQS delete) happens **after the store commits**, never after the pipeline | Tying the ack to pipeline latency makes a slow platform call redeliver the message. Shopee also throttles partners whose push success rate drops. |
| P6 | Dedup at ingest via unique `(platform, dedup_key)` + `ON CONFLICT DO NOTHING` | The event `id` is a fresh uuid per delivery, so it can never be the conflict target. |
| P7 | **No `platform_executions` table.** `jobs.target_quantity`, persisted before the write, is the idempotency mechanism | Read-modify-write is not idempotent; a retry must rewrite the same absolute value rather than recompute from fresh stock. |
| P8 | **No per-seller advisory lock** in the claim | Dropped from the optimal shape to keep the worker simple. Revisit if one seller's jobs interleave harmfully. |
| P9 | Reaper folded into the claim query, not a separate process | One statement, no second scheduler. |
| P10 | Migrations are a release step (`scripts/migrate.py`), never in-process | A second process makes `alembic upgrade` in `lifespan` a race, and a worker can boot against an unmigrated DB. |
| P11 | Seller-DM reorder replies with an **acknowledgement**; the worker posts the outcome as a follow-up | The API process holds nothing after commit, so it cannot report a result it never waits for. |
| P12 | **The write half is out of scope for now.** The boundary Protocol is `InboundAdapter` — `verify` + `normalize` only. The pipeline stops after the decision: after the Slack round trip, or after an EXECUTED decision when no approval was needed | Ships the structural half (transport, identity, dedup, fan-out) without the half that spends money. `get_stock`/`set_stock`/`classify_error`/`supports_stock_write` and the contract in [events-and-actions.md](events-and-actions.md) remain agreed — they are unbuilt, not undecided. |

## Scale posture

**No Kafka / RabbitMQ.** Postgres already holds the durable log; a broker on the ingest path adds a
second log plus a dual-write gap between `create_event()` and `producer.send()` — reintroducing the
loss it is meant to fix. Throughput is not the constraint: a worker blocking on a platform call does
~1–5 events/sec and SP-API token buckets cap writes well below what Postgres struggles with.

**Revisit when** the second and third platform adapters land *and* 3+ independent consumers read the
same stream. The trigger is consumer fan-out, not an events-per-second number. Item 8's outbox is the
prerequisite that makes the migration a config change.

**No per-seller partitioning or message ordering.** Neither platform documents any ordering guarantee
and SQS standard queues are explicitly unordered, so partitioning would impose an ordering the sources
never provided. If ordering becomes a real requirement, enforce it with a hold flag checked in the
policy engine — a state check cannot be defeated by an out-of-order delivery.

## Deferred, named rather than dropped

| Item | Why not now |
|---|---|
| A4 enrichment stage | Neither push is a sufficient audit record, but nothing depends on it yet. |
| A6 SQS consumer | The transport that makes Amazon ingest real. Blocked on nothing but scope. |
| A7 `accepted` + `transactionId` polling | Needed once an async platform write exists; the stock write is synchronous. |
| A9 scheduled signal generator | `inventory_low` polling and refund-rate derivation. |
| A10 platform-scoped concurrency | Stops being theoretical the moment detection produces real L2 volume. |
| Items 7, 8, 11–13 | Pool split, outbox, token caching, httpx timeouts, duplicate approval card. |
