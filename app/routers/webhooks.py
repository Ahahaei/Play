import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response

from app import store
from app.engine.pipeline import run_job
from app.models.event import DOMAIN_EVENT_TYPES, EventInput, EventStatus
from app.platforms import registry
from app.platforms.base import NormalizationError, RawDelivery, UnknownPlatform
from app.routers.deps import require_internal_token

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


# Declared before /{platform} so the literal path always wins the match.
@router.post("/sp-api", status_code=202, dependencies=[Depends(require_internal_token)])
def receive_sp_api_event(event: EventInput, background_tasks: BackgroundTasks):
    """Internal endpoint: inject a domain event with a trusted seller_id.

    Retained for tests and manual injection. Real platform deliveries go to
    /webhooks/{platform}, where the seller is resolved rather than asserted.
    """
    if event.event_type not in DOMAIN_EVENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"'{event.event_type}' is not a domain event. Use POST /events for monitoring events.",
        )
    enqueued = store.ingest_internal_event(event.seller_id, event.event_type, event.payload)
    background_tasks.add_task(run_job, enqueued.job_id, enqueued.event_id)
    return {"event_id": enqueued.event_id, "status": EventStatus.PENDING}


@router.post("/{platform}", status_code=202)
async def receive_platform_delivery(
    platform: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Verify → normalize → resolve → store, then acknowledge.

    Everything left of the store is terminal on failure: a bad signature never
    becomes good and an unparseable body never parses. The acknowledgement
    happens after the commit, never after the pipeline — tying it to pipeline
    latency makes a slow platform call redeliver the message, and Shopee
    throttles partners whose push success rate drops.
    """
    try:
        adapter = registry.get(platform)
    except UnknownPlatform:
        raise HTTPException(status_code=404, detail=f"Unknown platform '{platform}'")

    raw = RawDelivery(
        body=await request.body(),
        headers=dict(request.headers),
        query=dict(request.query_params),
        url=str(request.url),
    )

    if not adapter.verify(raw):
        raise HTTPException(status_code=401, detail="Delivery failed verification")

    try:
        events = adapter.normalize(raw)
    except NormalizationError as exc:
        logger.error("platform=%s poison delivery: %s body=%r", platform, exc, raw.body[:500])
        raise HTTPException(status_code=422, detail=str(exc))

    if not events:
        # A real transition we do not track. Acknowledged, stored nowhere.
        return Response(status_code=202)

    # Every event from one delivery belongs to one platform account.
    external_id = events[0].external_id
    account = store.get_account(platform, external_id)
    if account is None:
        # Dead-letter. Deliberately a 2xx: retrying cannot help, and a non-2xx
        # counts against Shopee's per-partner push success rate.
        logger.warning(
            "platform=%s unknown account external_id=%s — delivery dropped", platform, external_id
        )
        return Response(status_code=202)

    for enqueued in store.ingest_delivery(account.seller_id, platform, events):
        background_tasks.add_task(run_job, enqueued.job_id, enqueued.event_id)

    # Empty body: Shopee requires it, nothing else objects.
    return Response(status_code=202)
