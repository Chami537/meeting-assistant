"""Request and response bodies of the Worker API (design doc §5.1, §5.2)."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from .enums import ConflictReason, FailureCode, HeartbeatCommand, WorkerEventType

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SegmentNo = Annotated[int, Field(ge=0, le=2**31 - 1)]  # per attempt, consecutive from 0
AudioFormat = Annotated[str, Field(pattern=r"^[a-z0-9]{1,8}$")]
SILENCE_FLOOR_DBFS = -999.9  # NUMERIC(5,1); digital silence (-inf dBFS) is reported as this floor


class TaskAssignment(BaseModel):
    """Meeting credentials are sent only here; the Worker keeps them in memory."""

    task_id: UUID
    meeting_code: str | None
    join_url: str | None
    meeting_password: str | None
    display_name: str
    scheduled_at: datetime
    planned_end_at: datetime
    max_record_minutes: int


class LeaseResponse(BaseModel):
    attempt_id: UUID
    attempt_no: int
    lease_until: datetime
    server_time: datetime  # lets the Worker correct its clock for recorded_*_at
    last_event_seq: int  # a restarted Worker resumes its event numbering after this
    last_sealed_segment_no: int | None
    task: TaskAssignment


class HeartbeatResponse(BaseModel):
    lease_until: datetime
    command: HeartbeatCommand


class WorkerEvent(BaseModel):
    seq: int = Field(ge=1)  # must equal last_event_seq + 1; a repeat is acknowledged idempotently
    type: WorkerEventType
    occurred_at: datetime
    failure_code: FailureCode | None = None
    data: dict = Field(default_factory=dict)  # never links, passwords or transcript text

    @model_validator(mode="after")
    def _failed_needs_code(self) -> "WorkerEvent":
        # Without a code the backend cannot tell a host's refusal from a retryable glitch.
        if self.type == WorkerEventType.FAILED and self.failure_code is None:
            raise ValueError("FAILED events must carry failure_code")
        if self.type != WorkerEventType.FAILED and self.failure_code is not None:
            raise ValueError("failure_code is only allowed on FAILED events")
        if self.type == WorkerEventType.SEGMENT_SEALED:
            segment_no = self.data.get("segment_no")
            if not isinstance(segment_no, int) or isinstance(segment_no, bool) or not 0 <= segment_no < 2**31:
                raise ValueError("SEGMENT_SEALED needs data.segment_no in [0, 2**31)")
        return self


class SegmentUploadRequest(BaseModel):
    file_sha256: Sha256Hex
    bytes: int = Field(gt=0, le=2**40)
    format: AudioFormat
    duration_ms: int = Field(gt=0, le=2**31 - 1)
    recorded_started_at: datetime
    recorded_ended_at: datetime


class SegmentUploadResponse(BaseModel):
    object_key: str
    upload_url: str | None = None  # single PUT for small segments
    # Headers the PUT must send unchanged (e.g. x-cos-meta-sha256); they are part of the signature, so COS
    # refuses an upload whose size or declared hash differs from the registration.
    upload_headers: dict[str, str] = Field(default_factory=dict)
    cos_upload_id: str | None = None  # multipart upload, resumable
    credentials_expire_at: datetime


class SegmentCompleteRequest(BaseModel):
    file_sha256: Sha256Hex
    bytes: int = Field(gt=0, le=2**40)
    # Loudness measured by the Worker, for empty-audio checks. Finite only: digital silence is sent as
    # SILENCE_FLOOR_DBFS.
    rms_dbfs: float | None = Field(default=None, le=0, ge=SILENCE_FLOOR_DBFS, allow_inf_nan=False)


class ConflictResponse(BaseModel):
    """Body of every 409. EXPIRED/FINISHED: stop, keep sealed segments and upload them. CANCELED/NOT_OWNER:
    stop and delete local segments. OUT_OF_ORDER: keep going, resend events starting at expected_seq."""

    reason: ConflictReason
    detail: str = ""
    expected_seq: int | None = None
