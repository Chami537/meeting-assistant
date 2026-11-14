"""Worker API (design doc §5.1, §5.2). The Worker's identity comes from its token, never from the URL."""

from collections.abc import Awaitable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import JSONResponse
from meeting_protocol.worker_api import (
    ConflictResponse,
    HeartbeatResponse,
    LeaseResponse,
    SegmentCompleteRequest,
    SegmentUploadRequest,
    SegmentUploadResponse,
    WorkerEvent,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from ..deps import clock, db_engine, keyring, object_storage
from ..security.keyring import Keyring
from ..security.worker_auth import current_worker
from ..services import dispatch, uploads
from ..services.task_transitions import in_transaction_with_retry
from ..services.worker_errors import UploadRejected, WorkerConflict

router = APIRouter(prefix="/v1/workers")

SegmentPath = Annotated[int, Path(ge=0, le=2**31 - 1)]
CONFLICT = {409: {"model": ConflictResponse}}


async def _guard[T](call: Awaitable[T]) -> T | JSONResponse:
    """Protocol errors become 409 / 422 responses; anything else is a real 500."""
    try:
        return await call
    except WorkerConflict as exc:
        body = ConflictResponse(reason=exc.reason, detail=exc.detail, expected_seq=exc.expected_seq)
        return JSONResponse(status_code=409, content=body.model_dump())
    except UploadRejected as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})


@router.post("/lease", response_model=LeaseResponse, responses={204: {}, **CONFLICT})
async def lease(
    worker_id: UUID = Depends(current_worker),
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
    ring: Keyring = Depends(keyring),
):
    result = await _guard(
        in_transaction_with_retry(engine, lambda conn: dispatch.lease(conn, worker_id, now=now, keyring=ring))
    )
    if result is None:
        return Response(status_code=204)
    if isinstance(result, Response):
        return result
    return LeaseResponse(
        attempt_id=result.attempt_id,
        attempt_no=result.attempt_no,
        lease_until=result.lease_until,
        server_time=now,
        last_event_seq=result.last_event_seq,
        last_sealed_segment_no=result.last_sealed_segment_no,
        task=result.task,
    )


@router.post("/attempts/{attempt_id}/heartbeat", response_model=HeartbeatResponse, responses=CONFLICT)
async def heartbeat(
    attempt_id: UUID,
    worker_id: UUID = Depends(current_worker),
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
):
    result = await _guard(
        in_transaction_with_retry(
            engine, lambda conn: dispatch.heartbeat(conn, worker_id, attempt_id, now=now)
        )
    )
    if isinstance(result, Response):
        return result
    lease_until, command = result
    return HeartbeatResponse(lease_until=lease_until, command=command)


@router.post("/attempts/{attempt_id}/events", responses=CONFLICT)
async def events(
    attempt_id: UUID,
    event: WorkerEvent,
    worker_id: UUID = Depends(current_worker),
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
):
    result = await _guard(
        in_transaction_with_retry(
            engine, lambda conn: dispatch.record_worker_event(conn, worker_id, attempt_id, event, now=now)
        )
    )
    if isinstance(result, Response):
        return result
    return {"accepted": result}


@router.post(
    "/attempts/{attempt_id}/segments/{segment_no}/upload-url",
    response_model=SegmentUploadResponse,
    responses=CONFLICT,
)
async def upload_url(
    attempt_id: UUID,
    segment_no: SegmentPath,
    req: SegmentUploadRequest,
    worker_id: UUID = Depends(current_worker),
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
    storage: uploads.ObjectStorage = Depends(object_storage),
):
    result = await _guard(
        in_transaction_with_retry(
            engine,
            lambda conn: uploads.request_upload(
                conn, storage, worker_id, attempt_id, segment_no, req, now=now
            ),
        )
    )
    if isinstance(result, Response):
        return result
    return SegmentUploadResponse(
        object_key=result.object_key,
        upload_url=result.url,
        upload_headers=result.headers,
        credentials_expire_at=result.expires_at,
    )


@router.post("/attempts/{attempt_id}/segments/{segment_no}/complete", responses=CONFLICT)
async def complete(
    attempt_id: UUID,
    segment_no: SegmentPath,
    req: SegmentCompleteRequest,
    worker_id: UUID = Depends(current_worker),
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
    storage: uploads.ObjectStorage = Depends(object_storage),
):
    result = await _guard(
        uploads.complete_upload(
            lambda fn: in_transaction_with_retry(engine, fn),
            storage,
            worker_id,
            attempt_id,
            segment_no,
            req,
            now=now,
        )
    )
    if isinstance(result, Response):
        return result
    return {"status": "READY"}
