"""One-time result links (design doc §5.5).

A link carries a random token; only its SHA-256 is stored. GET never consumes it (WeChat's link scanner and
previews fetch every URL). The user's POST redeems it atomically and receives a session cookie bound to that
token row, valid SESSION_TTL; the page is then served against the cookie. Every view, delete request and
refused access is written to result_access_logs (client IP as a keyed hash).
"""

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from meeting_protocol import TaskStatus
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from ..models.tables import (
    kf_users,
    meeting_minutes,
    meeting_tasks,
    result_access_logs,
    result_access_tokens,
    task_inputs,
    transcripts,
)
from ..postprocess.minutes import meeting_title
from ..security.keyring import Keyring

LINK_TTL = timedelta(hours=24)
SESSION_TTL = timedelta(days=7)
TOKEN_PATTERN = r"^[A-Za-z0-9_-]{43}$"  # secrets.token_urlsafe(32)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def mint_link(
    conn: AsyncConnection, *, task_id: UUID, user_id: UUID, now: datetime, base_url: str
) -> str:
    """Called by the sender right before the message goes out; the token itself is never stored."""
    token = await _mint(conn, task_id=task_id, user_id=user_id, now=now, purpose="RESULT")
    return f"{base_url.rstrip('/')}/r/{token}"


async def mint_upload_link(
    conn: AsyncConnection, *, task_id: UUID, user_id: UUID, now: datetime, base_url: str
) -> str:
    """A one-time link to the upload page of a USER_UPLOAD task (design doc §4 路径 D)."""
    token = await _mint(conn, task_id=task_id, user_id=user_id, now=now, purpose="UPLOAD")
    return f"{base_url.rstrip('/')}/u/{token}"


async def _mint(conn: AsyncConnection, *, task_id: UUID, user_id: UUID, now: datetime, purpose: str) -> str:
    token = secrets.token_urlsafe(32)
    await conn.execute(
        insert(result_access_tokens).values(
            task_id=task_id,
            user_id=user_id,
            token_hash=_hash(token),
            expires_at=now + LINK_TTL,
            purpose=purpose,
        )
    )
    return token


@dataclass(frozen=True)
class UploadSlot:
    token_id: UUID
    task_id: UUID
    user_id: UUID


async def upload_slot(
    conn: AsyncConnection, token: str, *, now: datetime, lock: bool = False
) -> UploadSlot | None:
    """The task an unused, unexpired upload link belongs to, while that task still waits for its file."""
    r, t = result_access_tokens.c, meeting_tasks.c
    query = (
        select(r.id, r.task_id, r.user_id)
        .join(meeting_tasks, t.id == r.task_id)
        .where(
            r.token_hash == _hash(token),
            r.purpose == "UPLOAD",
            r.used_at.is_(None),
            r.expires_at > now,
            r.revoked_at.is_(None),
            t.status == TaskStatus.AWAITING_DETAILS,
        )
    )
    if lock:
        query = query.with_for_update(of=result_access_tokens)
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else UploadSlot(row.id, row.task_id, row.user_id)


async def upload_received(conn: AsyncConnection, token: str) -> TaskStatus | None:
    """For a link whose task already received its file (through this link or a sibling link), the task's
    status, so the page can say "being processed" or "done" instead of "expired"; otherwise None."""
    r, t = result_access_tokens.c, meeting_tasks.c
    sibling = result_access_tokens.alias("sibling")
    s = sibling.c
    status = await conn.scalar(
        select(t.status)
        .select_from(result_access_tokens.join(meeting_tasks, t.id == r.task_id))
        .where(
            r.token_hash == _hash(token),
            r.purpose == "UPLOAD",
            select(s.id)
            .where(s.task_id == r.task_id, s.purpose == "UPLOAD", s.used_at.is_not(None))
            .exists(),
        )
    )
    return None if status is None else TaskStatus(status)


async def use_upload_slot(conn: AsyncConnection, slot: UploadSlot, *, now: datetime) -> bool:
    """Mark this link used and revoke the task's other upload links: one task, one recording."""
    r = result_access_tokens.c
    result = await conn.execute(
        update(result_access_tokens)
        .where(r.id == slot.token_id, r.used_at.is_(None), r.revoked_at.is_(None))
        .values(used_at=now)
    )
    if result.rowcount != 1:
        return False
    await conn.execute(
        update(result_access_tokens)
        .where(
            r.task_id == slot.task_id,
            r.purpose == "UPLOAD",
            r.id != slot.token_id,
            r.used_at.is_(None),
            r.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return True


@dataclass(frozen=True)
class Session:
    token_id: UUID
    task_id: UUID
    secret: str
    expires_at: datetime


async def redeem(conn: AsyncConnection, token: str, *, now: datetime) -> Session | None:
    r = result_access_tokens.c
    secret = secrets.token_urlsafe(32)
    expires_at = now + SESSION_TTL
    row = (
        await conn.execute(
            update(result_access_tokens)
            .where(
                r.token_hash == _hash(token),
                r.purpose == "RESULT",
                r.used_at.is_(None),
                r.expires_at > now,
                r.revoked_at.is_(None),
            )
            .values(used_at=now, session_hash=_hash(secret), session_expires_at=expires_at)
            .returning(r.id, r.task_id)
        )
    ).one_or_none()
    return None if row is None else Session(row.id, row.task_id, secret, expires_at)


async def find_token(conn: AsyncConnection, token: str) -> tuple[UUID, UUID] | None:
    """(token id, task id) of a known token in any state, for logging refused redemptions."""
    r = result_access_tokens.c
    row = (
        await conn.execute(select(r.id, r.task_id).where(r.token_hash == _hash(token), r.purpose == "RESULT"))
    ).one_or_none()
    return None if row is None else (row.id, row.task_id)


async def check_session(
    conn: AsyncConnection, task_id: UUID, secret: str | None, *, now: datetime
) -> UUID | None:
    """The token row behind a valid session cookie for this task, or None."""
    if not secret:
        return None
    r = result_access_tokens.c
    return await conn.scalar(
        select(r.id).where(
            r.task_id == task_id,
            r.session_hash == _hash(secret),
            r.session_expires_at > now,
            r.revoked_at.is_(None),
        )
    )


async def log_access(
    conn: AsyncConnection,
    *,
    task_id: UUID,
    token_id: UUID | None,
    action: str,
    ip: str | None,
    user_agent: str | None,
    keyring: Keyring,
) -> None:
    await conn.execute(
        insert(result_access_logs).values(
            task_id=task_id,
            token_id=token_id,
            action=action,
            ip_hash=keyring.mac(ip.encode(), label="result-ip") if ip else None,
            user_agent=(user_agent or "")[:200] or None,
        )
    )


@dataclass(frozen=True)
class ResultView:
    task_id: UUID
    user_tag: str  # last 4 characters of the user's id, for the watermark
    scheduled_at: datetime | None
    timezone: str
    recording_outcome: str | None
    summary: str
    decisions: list[dict[str, Any]]
    action_items: list[dict[str, Any]]
    risks: list[dict[str, Any]]
    segments: list[dict[str, Any]]
    model: str
    # minutes-v2 sections (empty for minutes written before)
    key_points: list[dict[str, Any]] = field(default_factory=list)
    key_facts: list[dict[str, Any]] = field(default_factory=list)
    outlook: list[dict[str, Any]] = field(default_factory=list)
    name_fixes: list[dict[str, Any]] = field(default_factory=list)
    # minutes-v3: how the minutes are filed (None for minutes written before)
    meeting_title: str | None = None
    meeting_type: str | None = None
    meeting_date: date | None = None
    meeting_date_source: str | None = None


async def load_view(conn: AsyncConnection, task_id: UUID, *, now: datetime) -> ResultView | None:
    """None when there is nothing to show: not completed, due for purge or purged, or minutes deleted."""
    t, u, m, tr = meeting_tasks.c, kf_users.c, meeting_minutes.c, transcripts.c
    row = (
        await conn.execute(
            select(
                t.status,
                t.scheduled_at,
                t.timezone,
                t.recording_outcome,
                t.content_purged_at,
                t.retention_until,
                u.external_userid,
                m.summary,
                m.decisions,
                m.action_items,
                m.risks,
                m.key_points,
                m.key_facts,
                m.outlook,
                m.name_fixes,
                m.model_provider,
                m.model_name,
                m.meeting_type,
                m.meeting_date,
                m.meeting_date_source,
                tr.body,
                task_inputs.c.file_name,
            )
            .select_from(
                meeting_tasks.join(kf_users, u.id == t.user_id)
                .join(meeting_minutes, (m.task_id == t.id) & m.is_current)
                .join(transcripts, tr.id == m.transcript_id)
                .outerjoin(task_inputs, task_inputs.c.task_id == t.id)
            )
            .where(t.id == task_id)
        )
    ).one_or_none()
    if row is None or row.status != TaskStatus.COMPLETED or row.content_purged_at is not None:
        return None
    if row.retention_until is not None and row.retention_until <= now:
        return None
    return ResultView(
        task_id=task_id,
        user_tag=row.external_userid[-4:],
        scheduled_at=row.scheduled_at,
        timezone=row.timezone,
        recording_outcome=row.recording_outcome,
        summary=row.summary,
        decisions=row.decisions,
        action_items=row.action_items,
        risks=row.risks,
        segments=row.body.get("segments", []),
        model="/".join(filter(None, (row.model_provider, row.model_name))),
        key_points=row.key_points,
        key_facts=row.key_facts,
        outlook=row.outlook,
        name_fixes=row.name_fixes,
        meeting_title=meeting_title(row.file_name),
        meeting_type=row.meeting_type,
        meeting_date=row.meeting_date,
        meeting_date_source=row.meeting_date_source,
    )


async def request_deletion(conn: AsyncConnection, task_id: UUID, *, now: datetime) -> None:
    """The user deletes this meeting from the page: links and sessions stop working at once, the retention
    purge removes audio, transcript and minutes on its next pass."""
    r, t = result_access_tokens.c, meeting_tasks.c
    await conn.execute(
        update(result_access_tokens)
        .where(r.task_id == task_id, r.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await conn.execute(update(meeting_tasks).where(t.id == task_id).values(retention_until=now))
