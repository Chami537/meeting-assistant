"""Recordings handed in on the fallback path (design doc §4 路径 D) through the upload page or the
operator CLI.

The source file is staged in COS under the task's audio prefix (so a cancel's DISCARD_AUDIO also removes it),
recorded in task_inputs, and the task moves AWAITING_DETAILS -> AWAITING_CONSENT -> UPLOADING in one
transaction together with the consent record. The AUDIO_NORMALIZE job then probes, converts and hands the
audio to transcription (postprocess/jobs.py).
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from meeting_protocol import TaskKind, TaskStatus
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..domain.state_machine import InvalidTransition, Trigger, TriggerType
from ..intake import material
from ..intake import messages as msgs
from ..models.tables import kf_users, meeting_tasks
from ..postprocess.audio import BlobStore
from ..security.keyring import Keyring
from . import results
from .consent import record_requester_consent
from .task_inputs import InputKind, put_input
from .task_transitions import apply_trigger, in_transaction_with_retry
from .uploads import apply_and_settle

log = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 2 * 1024**3  # source files (video included); the converted audio is far smaller


class UploadError(Exception):
    """Shown to the uploader as is (Chinese)."""


def staged_key(task_id: UUID, upload_id: UUID | str) -> str:
    """Each upload attempt writes its own object, so a later or concurrent upload can never replace one
    that was accepted. Under the task's audio prefix: a cancel's DISCARD_AUDIO also removes it."""
    return f"tasks/{task_id}/audio/source-{upload_id}"


def check_media_head(path: Path) -> None:
    with path.open("rb") as f:
        head = f.read(material.HEAD_BYTES)
    if material.classify(head) != material.Kind.MEDIA:
        raise UploadError("这不是可识别的音频或视频文件，请上传 mp3、m4a、wav、mp4 等格式的会议录音。")


EXPIRED_LINK = "链接已失效。请在微信里回复「上传录音」获取新的链接。"


@dataclass(frozen=True)
class Accepted:
    task_id: UUID


async def accept_web_upload(
    engine: AsyncEngine,
    storage: BlobStore,
    keyring: Keyring,
    token: str,
    source: Path,
    *,
    file_name: str | None,
    consent_version: str,
    clock: Callable[[], datetime],
) -> Accepted:
    """The upload page: the user ticked the declaration and sent the file. Raises UploadError. The source is
    staged under a key of its own and removed again if it is not accepted; the commit uses the time after
    the transfer, so a long upload is not judged by the time it started."""
    if consent_version != msgs.UPLOAD_CONSENT_VERSION:
        raise UploadError("请刷新页面，确认最新的说明后再上传。")
    async with engine.connect() as conn:
        slot = await results.upload_slot(conn, token, now=clock())
    if slot is None:
        raise UploadError(EXPIRED_LINK)
    check_media_head(source)
    key = staged_key(slot.task_id, uuid.uuid4().hex)  # per attempt: a losing retry deletes only its own
    await storage.upload_file(source, key, content_type="application/octet-stream")

    async def commit(conn: AsyncConnection) -> Accepted:
        now = clock()
        locked = await results.upload_slot(conn, token, now=now, lock=True)
        if locked is None or not await results.use_upload_slot(conn, locked, now=now):
            raise UploadError(EXPIRED_LINK)
        await _stage_and_consent(
            conn,
            keyring,
            task_id=slot.task_id,
            user_id=slot.user_id,
            key=key,
            file_name=file_name,
            source_msg_type="web",
            channel="web_upload",
            evidence_ref=f"upload_token:{slot.token_id}",
            now=now,
        )
        return Accepted(slot.task_id)

    try:
        return await in_transaction_with_retry(engine, commit)
    except BaseException:
        await _forget(storage, key)
        raise


async def _forget(storage: BlobStore, key: str) -> None:
    try:
        await storage.delete(key)
    except Exception as exc:  # DISCARD_AUDIO / the purge remove it later
        log.warning("could not delete unaccepted upload %s: %s", key, type(exc).__name__)


async def _stage_and_consent(
    conn: AsyncConnection,
    keyring: Keyring,
    *,
    task_id: UUID,
    user_id: UUID,
    key: str,
    file_name: str | None,
    source_msg_type: str,
    channel: str,
    evidence_ref: str,
    now: datetime,
) -> None:
    status = await conn.scalar(
        select(meeting_tasks.c.status).where(meeting_tasks.c.id == task_id).with_for_update()
    )
    if status != TaskStatus.AWAITING_DETAILS:
        raise UploadError("这个上传任务已经结束，请在微信里回复「上传录音」重新开始。")
    await put_input(
        conn,
        keyring,
        task_id=task_id,
        kind=InputKind.STAGED_OBJECT,
        payload=key,
        source_msg_type=source_msg_type,
        file_name=(file_name or "")[:200] or None,
    )
    try:
        await apply_trigger(conn, task_id, Trigger(TriggerType.DETAILS_COMPLETE), now=now, actor=channel)
        await record_requester_consent(
            conn,
            task_id=task_id,
            user_id=user_id,
            channel=channel,
            evidence_ref=evidence_ref,
            version=msgs.UPLOAD_CONSENT_VERSION,
            text_sha256=msgs.UPLOAD_CONSENT_SHA256,
            now=now,
        )
        await apply_and_settle(
            conn,
            task_id,
            Trigger(TriggerType.CONSENT_GRANTED, consent_version=msgs.UPLOAD_CONSENT_VERSION),
            now=now,
            actor=channel,
        )
    except InvalidTransition as exc:
        raise UploadError("这个上传任务已经结束，请在微信里回复「上传录音」重新开始。") from exc


async def create_upload_task(conn: AsyncConnection, *, user_id: UUID, key: str, now: datetime) -> UUID:
    """A USER_UPLOAD task waiting for its file (AWAITING_DETAILS); `key` makes the request idempotent."""
    task_id = uuid.uuid4()
    await conn.execute(
        insert(meeting_tasks).values(
            id=task_id,
            user_id=user_id,
            task_kind=TaskKind.USER_UPLOAD,
            idempotency_key=key,
            status=TaskStatus.AWAITING_DETAILS,
        )
    )
    return task_id


async def stage_local_file(
    engine: AsyncEngine,
    storage: BlobStore,
    keyring: Keyring,
    source: Path,
    *,
    user_label: str,
    now: datetime,
) -> UUID:
    """Operator / demo path (no WeChat): a task for a demo user, consent recorded on the operator's word
    (channel api), source staged in COS. The runner's jobs take it from there."""
    check_media_head(source)
    size = source.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise UploadError("文件太大")

    async def create(conn: AsyncConnection) -> tuple[UUID, UUID]:
        u = kf_users.c
        user_id = await conn.scalar(select(u.id).where(u.corp_id == "demo", u.external_userid == user_label))
        if user_id is None:
            user_id = await conn.scalar(
                insert(kf_users).values(corp_id="demo", external_userid=user_label).returning(u.id)
            )
        task_id = await create_upload_task(conn, user_id=user_id, key=f"cli:{uuid.uuid4().hex}", now=now)
        return task_id, user_id

    task_id, user_id = await in_transaction_with_retry(engine, create)
    key = staged_key(task_id, uuid.uuid4().hex)
    await storage.upload_file(source, key, content_type="application/octet-stream")
    try:
        await in_transaction_with_retry(
            engine,
            lambda conn: _stage_and_consent(
                conn,
                keyring,
                task_id=task_id,
                user_id=user_id,
                key=key,
                file_name=source.name,
                source_msg_type="cli",
                channel="api",
                evidence_ref="operator-cli",
                now=now,
            ),
        )
    except BaseException:
        await _forget(storage, key)
        raise
    return task_id
