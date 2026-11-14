"""WeChat KF message intake (design doc §3): callback token handling and the sync_msg loop.

The callback only says "go pull"; we keep its token (valid 10 minutes, encrypted at rest) and pull with
sync_msg, page by page. Each page commits the inbox rows together with the new cursor while holding the
kf_sync_state row lock, so concurrent pulls never reuse a cursor. A message is stored twice over: a
redacted skeleton for processing and audits, and the original content encrypted (purged after 7 days).

An item that cannot be stored (a missing field, a value of the wrong type) is quarantined: kept encrypted as a
FAILED row and reported, so that one malformed item never rolls back its page and stalls the cursor for
everyone.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..intake import messages as msgs
from ..integrations.wecom.client import WeComApiError, WeComClient
from ..models.tables import kf_inbox, kf_sync_state, kf_users, outgoing_messages
from ..security.keyring import Keyring

log = logging.getLogger(__name__)

SYNC_TOKEN_TTL = timedelta(minutes=10)
INVALID_SYNC_TOKEN = 95007  # sync_msg errcode for a token that is not (or no longer) valid
MAX_PAGES_PER_RUN = 50

# sync_msg origin: 3 = sent by the WeChat customer, 4 = system event, 5 = sent by a human servicer
ORIGIN_CUSTOMER = 3
ORIGIN_EVENT = 4
_EVENTS_TO_PROCESS = {"enter_session", "msg_send_fail"}

# Kept in the redacted payload; everything else (text, links, cards, media ids, welcome codes) is encrypted.
_SAFE_KEYS = ("msgid", "open_kfid", "msgtype", "origin", "send_time")
_SAFE_EVENT_KEYS = ("event_type", "fail_type", "scene")
MALFORMED = "malformed"  # message_type of a quarantined item


def _token_aad(open_kfid: str) -> bytes:
    return f"kf_sync_state.pending_sync_token:{open_kfid}".encode()


def _content_aad(open_kfid: str, msg_id: str) -> bytes:
    return f"kf_inbox.content:{open_kfid}:{msg_id}".encode()


async def record_callback_token(
    conn: AsyncConnection, open_kfid: str, token: str, *, now: datetime, keyring: Keyring
) -> None:
    sealed = keyring.encrypt_text(token, aad=_token_aad(open_kfid))
    values = {
        "pending_sync_token_ciphertext": sealed.ciphertext,
        "pending_sync_token_key_version": sealed.key_version,
        "pending_sync_token_expires_at": now + SYNC_TOKEN_TTL,
    }
    stmt = pg_insert(kf_sync_state).values(open_kfid=open_kfid, **values)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["open_kfid"], set_=values))


def split_message(msg: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Returns (message_type, redacted, sensitive). message_type is msgtype, or event:<event_type>."""
    redacted = {k: msg[k] for k in _SAFE_KEYS if k in msg}
    sensitive = {k: v for k, v in msg.items() if k not in _SAFE_KEYS and k != "external_userid"}
    message_type = msg.get("msgtype", "unknown")
    if message_type == "event":
        event = msg.get("event", {})
        redacted["event"] = {k: event[k] for k in _SAFE_EVENT_KEYS if k in event}
        message_type = f"event:{event.get('event_type', 'unknown')}"
    return message_type, redacted, sensitive


def _initial_status(origin: int | None, message_type: str) -> str:
    if origin == ORIGIN_CUSTOMER:
        return "PENDING"
    if message_type.removeprefix("event:") in _EVENTS_TO_PROCESS:
        return "PENDING"
    return "IGNORED"  # servicer messages and events we do not act on


async def _upsert_user(
    conn: AsyncConnection, corp_id: str, external_userid: str, *, inbound_at: datetime | None, now: datetime
) -> UUID:
    u = kf_users.c
    stmt = pg_insert(kf_users).values(
        corp_id=corp_id, external_userid=external_userid, last_seen_at=now, last_inbound_at=inbound_at
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["corp_id", "external_userid"],
        set_={
            "last_seen_at": now,
            "last_inbound_at": func.greatest(u.last_inbound_at, stmt.excluded.last_inbound_at),
        },
    ).returning(u.id)
    return await conn.scalar(stmt)


async def store_message(
    conn: AsyncConnection, corp_id: str, msg: dict[str, Any], *, now: datetime, keyring: Keyring
) -> bool:
    """Insert one sync_msg item; False if (open_kfid, msgid) was already stored."""
    open_kfid, msg_id = msg["open_kfid"], msg["msgid"]
    if not (isinstance(open_kfid, str) and open_kfid and isinstance(msg_id, str) and msg_id):
        raise ValueError("open_kfid / msgid missing")
    origin = msg.get("origin")
    if origin is not None and not isinstance(origin, int):
        raise ValueError("origin is not an integer")
    message_type, redacted, sensitive = split_message(msg)
    sent_at = _sent_at(msg.get("send_time"), now)
    external_userid = msg.get("external_userid") or msg.get("event", {}).get("external_userid")
    if external_userid is not None and not isinstance(external_userid, str):
        raise ValueError("external_userid is not a string")
    user_id = None
    if external_userid:
        inbound = sent_at if origin == ORIGIN_CUSTOMER else None
        user_id = await _upsert_user(conn, corp_id, external_userid, inbound_at=inbound, now=now)
    sealed = keyring.encrypt_text(
        json.dumps(sensitive, ensure_ascii=False, sort_keys=True), aad=_content_aad(open_kfid, msg_id)
    )
    inserted = await conn.execute(
        pg_insert(kf_inbox)
        .values(
            open_kfid=open_kfid,
            msg_id=msg_id,
            user_id=user_id,
            message_type=message_type,
            origin=origin,
            payload_redacted=redacted,
            content_ciphertext=sealed.ciphertext,
            content_key_version=sealed.key_version,
            status=_initial_status(origin, message_type),
            next_attempt_at=now,
            received_at=now,
        )
        .on_conflict_do_nothing(index_elements=["open_kfid", "msg_id"])
        .returning(kf_inbox.c.id)
    )
    return inserted.first() is not None


def _sent_at(send_time: Any, now: datetime) -> datetime:
    """send_time opens the reply window; a value we cannot read counts as now, rather than losing the item."""
    try:
        return datetime.fromtimestamp(int(send_time), tz=now.tzinfo)
    except (TypeError, ValueError, OverflowError, OSError):
        return now


def _storable(value: Any) -> bool:
    """A non-empty string PostgreSQL text accepts (no NUL, no lone surrogate)."""
    if not (isinstance(value, str) and value) or "\x00" in value:
        return False
    try:
        value.encode()
    except UnicodeEncodeError:
        return False
    return True


_ITEM_ERRORS = (KeyError, TypeError, ValueError, AttributeError, UnicodeError, RecursionError)


def _items_fault(exc: Exception) -> bool:
    """Only a problem with the item's data justifies setting it aside: a missing or mistyped field, or a value
    PostgreSQL refuses (SQLSTATE class 22 data exception, 23 constraint). Anything else (a deadlock, a lost
    connection, a cancelled query, a schema not yet migrated) rolls the page back to be pulled again."""
    if isinstance(exc, DBAPIError):
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
            getattr(exc.orig, "__cause__", None), "sqlstate", None
        )
        return isinstance(sqlstate, str) and sqlstate[:2] in ("22", "23")
    return isinstance(exc, _ITEM_ERRORS)


async def _store_or_quarantine(
    conn: AsyncConnection, corp_id: str, open_kfid: str, msg: Any, *, now: datetime, keyring: Keyring
) -> bool:
    """Each in its own savepoint, so that no item can roll back the page and keep the cursor where it is."""
    try:
        async with conn.begin_nested():
            return await store_message(conn, corp_id, msg, now=now, keyring=keyring)
    except Exception as exc:
        if not _items_fault(exc):
            raise
        error = type(exc).__name__
    try:
        async with conn.begin_nested():
            kept = await _quarantine(conn, corp_id, open_kfid, msg, now=now, keyring=keyring, error=error)
    except Exception as again:
        if not _items_fault(again):
            raise
        # Even keeping it failed: the item is lost, but every later message must still get through.
        log.error("sync_msg item of %s dropped: %s, then %s", open_kfid, error, type(again).__name__)
        return False
    if kept:
        log.error("sync_msg item of %s quarantined: %s", open_kfid, error)
    return False


async def _quarantine(
    conn: AsyncConnection,
    corp_id: str,
    open_kfid: str,
    msg: Any,
    *,
    now: datetime,
    keyring: Keyring,
    error: str,
) -> bool:
    """Keep an item that store_message rejected: encrypted, FAILED, never processed. Idempotent across pulls
    of the same page (keyed by its msgid, or by a hash of the item when that is unusable). Nothing here may
    fail on the item's content: it is serialized as ASCII, and the customer is only looked up, never created.
    A known customer is linked (so erasure covers the row) and told the message was not handled."""
    item = dict(msg) if isinstance(msg, dict) else msg
    external_userid = None
    if isinstance(item, dict):
        top = item.pop("external_userid", None)
        event = dict(item["event"]) if isinstance(item.get("event"), dict) else None
        nested = event.pop("external_userid", None) if event is not None else None
        if event is not None:
            item["event"] = event
        external_userid = top if _storable(top) else nested
    raw = json.dumps(item, ensure_ascii=True, sort_keys=True, default=str)
    msg_id = item.get("msgid") if isinstance(item, dict) else None
    if not _storable(msg_id) or msg_id.startswith(f"{MALFORMED}:"):
        # Keyed: a plain hash of a short text (a password, "同意") could be confirmed by guessing.
        msg_id = f"{MALFORMED}:{keyring.mac(raw.encode(), label='kf_inbox.malformed')[:32]}"
    customer = isinstance(item, dict) and item.get("origin") == ORIGIN_CUSTOMER
    user_id = None
    if _storable(external_userid):
        u = kf_users.c
        user_id = await conn.scalar(
            select(u.id).where(u.corp_id == corp_id, u.external_userid == external_userid)
        )
        if user_id is not None and customer:  # the customer did write: their reply window is open
            sent_at = min(_sent_at(item.get("send_time"), now), now)
            await conn.execute(
                update(kf_users)
                .where(u.id == user_id)
                .values(last_seen_at=now, last_inbound_at=func.greatest(u.last_inbound_at, sent_at))
            )
    sealed = keyring.encrypt_text(raw, aad=_content_aad(open_kfid, msg_id))
    inserted = await conn.execute(
        pg_insert(kf_inbox)
        .values(
            open_kfid=open_kfid,
            msg_id=msg_id,
            user_id=user_id,
            message_type=MALFORMED,
            payload_redacted={"error": error},
            content_ciphertext=sealed.ciphertext,
            content_key_version=sealed.key_version,
            status="FAILED",
            last_error=error,
            next_attempt_at=now,
            received_at=now,
        )
        .on_conflict_do_nothing(index_elements=["open_kfid", "msg_id"])
        .returning(kf_inbox.c.id)
    )
    if inserted.first() is None:
        return False
    if user_id is not None and customer:
        await conn.execute(
            pg_insert(outgoing_messages)
            .values(
                user_id=user_id,
                open_kfid=open_kfid,
                purpose="MESSAGE_FAILED",
                dedup_key=f"reply:{msg_id}:MESSAGE_FAILED",
                content=msgs.message_failed(now),
                next_attempt_at=now,
            )
            .on_conflict_do_nothing(index_elements=["dedup_key"])
        )
    return True


def open_content(row_ciphertext: bytes, open_kfid: str, msg_id: str, keyring: Keyring) -> dict[str, Any]:
    return json.loads(keyring.decrypt_text(row_ciphertext, aad=_content_aad(open_kfid, msg_id)))


async def sync_account(
    engine: AsyncEngine, client: WeComClient, *, corp_id: str, open_kfid: str, now: datetime, keyring: Keyring
) -> int:
    """Pull until has_more is false. An empty page with has_more=1 is possible, so only has_more stops it.
    Returns the number of new messages."""
    stored = 0
    for _ in range(MAX_PAGES_PER_RUN):
        async with engine.begin() as conn:
            s = kf_sync_state.c
            await conn.execute(pg_insert(kf_sync_state).values(open_kfid=open_kfid).on_conflict_do_nothing())
            state = (
                await conn.execute(
                    select(
                        s.next_cursor,
                        s.pending_sync_token_ciphertext,
                        s.pending_sync_token_expires_at,
                    )
                    .where(s.open_kfid == open_kfid)
                    .with_for_update()
                )
            ).one()
            token = None
            if state.pending_sync_token_ciphertext and state.pending_sync_token_expires_at > now:
                token = keyring.decrypt_text(state.pending_sync_token_ciphertext, aad=_token_aad(open_kfid))
            try:
                page = await client.sync_msg(open_kfid=open_kfid, cursor=state.next_cursor, token=token)
            except WeComApiError as exc:
                if token is None or exc.errcode != INVALID_SYNC_TOKEN:
                    raise
                token = None  # stale callback token: pull without it (rate limited) and forget it
                page = await client.sync_msg(open_kfid=open_kfid, cursor=state.next_cursor, token=None)
            for msg in page.msg_list:  # in msg_list order: ingest_seq records the order of arrival
                stored += await _store_or_quarantine(conn, corp_id, open_kfid, msg, now=now, keyring=keyring)
            done = not page.has_more
            await conn.execute(
                kf_sync_state.update()
                .where(s.open_kfid == open_kfid)
                .values(
                    next_cursor=page.next_cursor or state.next_cursor,
                    last_synced_at=now,
                    **(
                        {
                            "pending_sync_token_ciphertext": None,
                            "pending_sync_token_key_version": None,
                            "pending_sync_token_expires_at": None,
                        }
                        if done or token is None
                        else {}
                    ),
                )
            )
        if done:
            break
    return stored
