from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app import store
from app.engine.pipeline import run_job
from app.models.event import MONITORING_EVENT_TYPES, EventInput, EventStatus
from app.routers.deps import require_internal_token

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", status_code=202, dependencies=[Depends(require_internal_token)])
def ingest_event(event: EventInput, background_tasks: BackgroundTasks):
    """Internal endpoint: inject a monitoring signal with a trusted seller_id.

    Reaches the auto-execute path, hence the shared secret. Signals will
    eventually be produced by detectors rather than posted here.
    """
    if event.event_type not in MONITORING_EVENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"'{event.event_type}' is not a monitoring event. Use POST /webhooks/sp-api for domain events.",
        )
    enqueued = store.ingest_internal_event(event.seller_id, event.event_type, event.payload)
    background_tasks.add_task(run_job, enqueued.job_id, enqueued.event_id)
    return {"event_id": enqueued.event_id, "status": EventStatus.PENDING}


@router.get("/{event_id}")
def get_event(event_id: str):
    record = store.get_event(event_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Event '{event_id}' not found")
    return record
