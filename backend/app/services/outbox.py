"""Deliver outgoing_messages through 微信客服 within its limits (design doc §3, §5.4):
- only within 48 h of the customer's last message and at most 5 messages in that window;
- only while the session is 0 (未处理) or 1 (智能助手接待); a session with a human servicer is left alone;
- msgid = id hex (27 chars) + "g" + send_generation, written when the row is claimed, so msg_send_fail events
  and retries match it. A retry after a timeout keeps the msgid, and WeCom's "msgid 重复" then means the
  message was already accepted; a message released from DEFERRED is a new delivery with a new generation;
- a user's messages go out in order: once one of them fails or is deferred, the user's later ones wait;
- result and upload links are placeholders in the stored text; the one-time link is minted right before
  sending, so the token is never stored and a deferred message never carries an expired link (§5.5).
Messages that cannot go out now become DEFERRED and are released by the user's next message."""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from meeting_protocol import TaskStatus
from sqlalchemy import Text, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ..intake.messages import RESULT_LINK_PLACEHOLDER, UPLOAD_LINK_PLACEHOLDER
from ..integrations.wecom.client import WeComApiError, WeComClient
from ..models.tables import kf_users, meeting_tasks, outgoing_messages
from .results import mint_link, mint_upload_link

log = logging.getLogger(__name__)

REPLY_WINDOW = timedelta(hours=48)
REPLY_BUDGET = 5
SEND_LEASE = timedelta(minutes=2)
MAX_SEND_ATTEMPTS = 5
_BOT_STATES = {0, 1}
# send_msg errors that mean "not now" rather than "broken" (official error codes, doc 90313)
_DEFER_CODES = {
    95001: "reply budget (5 messages) used up",
    95002: "outside the 48 h reply window",
    95018: "session state does not allow sending",
}
_LINKS = {RESULT_LINK_PLACEHOLDER: mint_link, UPLOAD_LINK_PLACEHOLDER: mint_upload_link}
_MSGID_TAKEN = 95033  # "msgid 重复": an earlier try with this msgid was accepted


def msgid_expr():
    o = outgoing_messages.c
    hex_id = func.replace(cast(o.id, Text), "-", "")
    return func.substr(hex_id, 1, 27) + "g" + func.lpad(cast(o.send_generation, Text), 4, "0")


async def send_outbox(
    engine: AsyncEngine,
    client: WeComClient,
    *,
    now: datetime,
    base_url: str | None = None,
    limit: int = 20,
) -> int:
    o = outgoing_messages.c
    async with engine.begin() as conn:
        # A row whose sender died after its last allowed attempt is not retried forever.
        abandoned = (
            await conn.execute(
                update(outgoing_messages)
                .where(o.status == "SENDING", o.next_attempt_at <= now, o.attempts >= MAX_SEND_ATTEMPTS)
                .values(status="FAILED", last_error="no outcome after the last attempt")
                .returning(o.id, o.purpose)
            )
        ).all()
        for row in abandoned:
            log.error(
                "outgoing message %s (%s) gave up: no outcome after the last attempt", row.id, row.purpose
            )
        earlier = outgoing_messages.alias("earlier")
        e = earlier.c
        held_back = (
            select(e.id)
            .where(
                e.user_id == o.user_id,
                e.created_at < o.created_at,
                e.status.in_(("PENDING", "SENDING", "DEFERRED")),
                e.next_attempt_at > now,
            )
            .exists()
        )
        claimable = (
            select(o.id)
            .where(o.status.in_(("PENDING", "SENDING")), o.next_attempt_at <= now, ~held_back)
            .order_by(o.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (
            await conn.execute(
                update(outgoing_messages)
                .where(o.id.in_(claimable.scalar_subquery()))
                .values(
                    status="SENDING",
                    attempts=o.attempts + 1,
                    next_attempt_at=now + SEND_LEASE,
                    wecom_msgid=msgid_expr(),
                )
                .returning(
                    o.id,
                    o.task_id,
                    o.user_id,
                    o.open_kfid,
                    o.purpose,
                    o.content,
                    o.attempts,
                    o.created_at,
                    o.wecom_msgid,
                )
            )
        ).all()

    sent = 0
    blocked: set[UUID] = set()
    for row in sorted(rows, key=lambda r: r.created_at):
        if row.user_id in blocked:  # an earlier message to this user did not go out: keep the order
            await _set(engine, row, status="PENDING", attempts=row.attempts - 1, next_attempt_at=now)
            continue
        outcome = await _send_one(engine, client, row, now=now, base_url=base_url)
        if outcome == "SENT":
            sent += 1
        else:
            blocked.add(row.user_id)
    return sent


async def _send_one(
    engine: AsyncEngine, client: WeComClient, row, *, now: datetime, base_url: str | None
) -> str:
    o, u = outgoing_messages.c, kf_users.c
    async with engine.connect() as conn:
        user = (
            await conn.execute(select(u.external_userid, u.last_inbound_at).where(u.id == row.user_id))
        ).one()
        used = (
            await conn.scalar(
                select(func.count()).where(
                    o.user_id == row.user_id, o.status == "SENT", o.sent_at >= user.last_inbound_at
                )
            )
            if user.last_inbound_at
            else 0
        )
    in_window = user.last_inbound_at is not None and now - user.last_inbound_at < REPLY_WINDOW
    if not in_window or used >= REPLY_BUDGET:
        return await _set(engine, row, status="DEFERRED", last_error="outside reply window or budget")
    try:
        content = row.content
        for placeholder, mint in _LINKS.items():
            if placeholder not in content:
                continue
            if not base_url or row.task_id is None:
                reason = "PUBLIC_BASE_URL not configured" if not base_url else "link message without a task"
                log.error("outgoing message %s (%s) failed: %s", row.id, row.purpose, reason)
                return await _set(engine, row, status="FAILED", last_error=reason)
            async with engine.begin() as conn:
                if placeholder == UPLOAD_LINK_PLACEHOLDER and not await _waits_for_upload(conn, row.task_id):
                    link = None  # sent late (deferred): its task was cancelled or expired meanwhile
                else:
                    link = await mint(
                        conn, task_id=row.task_id, user_id=row.user_id, now=now, base_url=base_url
                    )
            if link is None:
                return await _set(engine, row, status="CANCELED", last_error="upload task no longer waiting")
            content = content.replace(placeholder, link)
        state = await client.service_state(open_kfid=row.open_kfid, external_userid=user.external_userid)
        if state not in _BOT_STATES:
            return await _set(engine, row, status="DEFERRED", last_error=f"session state {state}")
        if state == 0:
            await client.transfer_service_state(
                open_kfid=row.open_kfid, external_userid=user.external_userid, state=1
            )
        await client.send_text(
            open_kfid=row.open_kfid,
            external_userid=user.external_userid,
            content=content,
            msgid=row.wecom_msgid,
        )
    except WeComApiError as exc:
        if exc.errcode == _MSGID_TAKEN:
            return await _set(engine, row, status="SENT", sent_at=now, last_error=None)
        if exc.errcode in _DEFER_CODES:
            return await _set(engine, row, status="DEFERRED", last_error=f"{exc.errcode}")
        return await _retry(engine, row, now=now, error=exc.errmsg if exc.errcode == -1 else f"{exc.errcode}")
    except Exception as exc:  # network, database: the same msgid is retried, 95033 then settles it
        log.error("outgoing message %s failed: %s", row.id, type(exc).__name__)
        return await _retry(engine, row, now=now, error=type(exc).__name__)
    return await _set(engine, row, status="SENT", sent_at=now, last_error=None)


async def _retry(engine: AsyncEngine, row, *, now: datetime, error: str) -> str:
    final = row.attempts >= MAX_SEND_ATTEMPTS
    if final:  # the user will not get this message: someone has to look (errcode only, no content)
        log.error(
            "outgoing message %s (%s) gave up after %d attempts: %s", row.id, row.purpose, row.attempts, error
        )
    return await _set(
        engine,
        row,
        status="FAILED" if final else "PENDING",
        next_attempt_at=now + timedelta(seconds=30 * row.attempts),
        last_error=error,
    )


async def _set(engine: AsyncEngine, row, **values) -> str:
    """Only while this claim still owns the row (SENDING, same attempt count)."""
    o = outgoing_messages.c
    async with engine.begin() as conn:
        await conn.execute(
            update(outgoing_messages)
            .where(o.id == row.id, o.status == "SENDING", o.attempts == row.attempts)
            .values(**values)
        )
    return values["status"]


async def _waits_for_upload(conn, task_id) -> bool:
    t = meeting_tasks.c
    status = await conn.scalar(select(t.status).where(t.id == task_id))
    return status == TaskStatus.AWAITING_DETAILS


def release_deferred_values() -> dict:
    """A message released from DEFERRED is a new delivery: new msgid generation, fresh retry budget."""
    o = outgoing_messages.c
    return {
        "status": "PENDING",
        "attempts": 0,
        "send_generation": o.send_generation + 1,
        "wecom_msgid": None,
    }
