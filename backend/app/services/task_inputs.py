"""task_inputs: what the user handed in for a fallback task (a WeChat media_id, a staged COS object, or the
forwarded text), encrypted like message content and cleared once consumed or when the task ends."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ..models.tables import task_inputs
from ..security.keyring import Keyring


class InputKind(StrEnum):
    WECOM_MEDIA = "WECOM_MEDIA"
    STAGED_OBJECT = "STAGED_OBJECT"
    FORWARDED_TEXT = "FORWARDED_TEXT"


@dataclass(frozen=True)
class TaskInput:
    kind: InputKind
    payload: str | None  # None once consumed
    file_name: str | None
    source_msg_type: str | None
    expires_at: datetime | None
    consumed_at: datetime | None
    created_at: datetime


def _aad(task_id: UUID) -> bytes:
    return f"task_inputs.payload:{task_id}".encode()


async def put_input(
    conn: AsyncConnection,
    keyring: Keyring,
    *,
    task_id: UUID,
    kind: InputKind,
    payload: str,
    source_msg_type: str | None = None,
    file_name: str | None = None,
    expires_at: datetime | None = None,
) -> None:
    sealed = keyring.encrypt_text(payload, aad=_aad(task_id))
    await conn.execute(
        pg_insert(task_inputs).values(
            task_id=task_id,
            kind=kind,
            payload_ciphertext=sealed.ciphertext,
            payload_key_version=sealed.key_version,
            source_msg_type=source_msg_type,
            file_name=file_name,
            expires_at=expires_at,
        )
    )


async def read_input(
    conn: AsyncConnection, keyring: Keyring, task_id: UUID, *, lock: bool = False
) -> TaskInput | None:
    i = task_inputs.c
    query = select(
        i.kind,
        i.payload_ciphertext,
        i.file_name,
        i.source_msg_type,
        i.expires_at,
        i.consumed_at,
        i.created_at,
    ).where(i.task_id == task_id)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    payload = (
        keyring.decrypt_text(row.payload_ciphertext, aad=_aad(task_id)) if row.payload_ciphertext else None
    )
    return TaskInput(
        InputKind(row.kind),
        payload,
        row.file_name,
        row.source_msg_type,
        row.expires_at,
        row.consumed_at,
        row.created_at,
    )


async def replace_payload(
    conn: AsyncConnection, keyring: Keyring, task_id: UUID, payload: str, *, expires_at: datetime | None
) -> None:
    sealed = keyring.encrypt_text(payload, aad=_aad(task_id))
    i = task_inputs.c
    await conn.execute(
        update(task_inputs)
        .where(i.task_id == task_id)
        .values(
            payload_ciphertext=sealed.ciphertext,
            payload_key_version=sealed.key_version,
            expires_at=expires_at,
        )
    )


async def consume(conn: AsyncConnection, task_id: UUID, *, now: datetime) -> None:
    i = task_inputs.c
    await conn.execute(
        update(task_inputs)
        .where(i.task_id == task_id)
        .values(payload_ciphertext=None, payload_key_version=None, consumed_at=now)
    )


async def drop(conn: AsyncConnection, task_id: UUID) -> None:
    await conn.execute(delete(task_inputs).where(task_inputs.c.task_id == task_id))
