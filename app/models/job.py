from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    DEAD = "dead"


class JobKind(str, Enum):
    PIPELINE = "pipeline"


class Job(BaseModel):
    id: str
    event_id: str
    seller_id: str
    kind: JobKind
    status: JobStatus
    attempts: int
    run_after: datetime
    locked_at: Optional[datetime] = None
    target_quantity: Optional[int] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class EnqueuedEvent(BaseModel):
    """An event that was newly stored, with the job that will process it.

    Duplicates never produce one of these — a redelivery stores nothing and
    enqueues nothing.
    """

    event_id: str
    job_id: str
