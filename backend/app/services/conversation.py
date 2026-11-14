"""Turn KF inbox messages into tasks and replies (module 3, design doc §1, §5.4).

Each inbox row is claimed with SKIP LOCKED and handled in its own transaction under the kf_users row lock;
process_inbox applies one user's messages in the order they were pulled. Replies are written to
outgoing_messages (the outbox sender delivers them within the 48 h / 5 message budget).
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from meeting_protocol import TaskKind, TaskStatus
from meeting_protocol.rules import IN_ATTEMPT_STATUSES, TERMINAL_STATUSES
from sqlalchemy import func, insert, literal_column, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..domain.state_machine import DISPATCH_LEAD, InvalidTransition, Transition, Trigger, TriggerType
from ..intake import material
from ..intake import messages as msgs
from ..intake.invite_parser import InviteFlag, ParsedInvite, parse_miniprogram, parse_text, parse_time_range
from ..models.tables import (
    kf_inbox,
    kf_users,
    meeting_tasks,
    outgoing_messages,
    task_inputs,
    transcripts,
    worker_nodes,
)
from ..postprocess.minutes import TranscriptTooLong, build_user_prompt
from ..security.keyring import Keyring
from .consent import record_requester_consent
from .dispatch import INTAKE_TTL, credential_aad
from .kf_media import MediaPeek, Prefetched, prefetch
from .kf_sync import open_content
from .outbox import release_deferred_values
from .results import LINK_TTL
from .task_inputs import InputKind, put_input, read_input, replace_payload
from .task_inputs import consume as consume_input
from .task_transitions import in_transaction_with_retry
from .uploads import apply_and_settle

log = logging.getLogger(__name__)

INBOX_LEASE = timedelta(minutes=5)
MAX_INBOX_ATTEMPTS = 5
DEFAULT_DURATION = timedelta(hours=1)
OCCUPANCY_BEFORE = DISPATCH_LEAD
OCCUPANCY_AFTER = timedelta(minutes=30)

_OCCUPYING = (
    TaskStatus.SCHEDULED,
    TaskStatus.DISPATCHING,
    TaskStatus.JOINING,
    TaskStatus.WAITING_ROOM,
    TaskStatus.IN_MEETING,
    TaskStatus.AWAITING_RECORDING_APPROVAL,
    TaskStatus.RECORDING,
    TaskStatus.RETRY_WAIT,
)
_UNUSABLE_TIME = {InviteFlag.ENDS_BEFORE_START, InviteFlag.STARTS_IN_PAST, InviteFlag.TIME_UNCLEAR}
_AGREE = {"同意", "我同意", "确认", "好的同意", "agree", "yes"}
_CANCEL = {"取消", "取消任务", "取消会议", "cancel"}
_STOP = {"结束", "结束录音", "停止", "停止录音", "结束并出纪要", "stop"}
_NO_PASSWORD = {"无密码", "没有密码", "无", "没有"}
_DELETE = {"删除", "删除数据", "删除我的数据"}
_VIEW = {"查看纪要", "纪要", "看纪要", "查看会议纪要", "会议纪要"}
_UPLOAD = {"上传录音", "上传", "上传文件", "上传会议录音", "大文件", "录音太大", "文件太大"}
_FILE_TYPES = ("file", "video")
FORWARDED_MIN_CHARS = 200  # shorter unrecognised text is not taken for minutes or a transcript
APPEND_WINDOW = timedelta(minutes=30)
PREFETCH_TIMEOUT_S = 30  # one file's head (and a document's body) from WeChat
PREFETCH_BUDGET_S = 20  # per inbox pass, so downloads do not delay the rest of the runner tick
INTAKE_MEMORY = timedelta(hours=48)  # how long a silently dropped meeting request still explains a '同意'
_DONE_SENDING = {"发完了", "没有了", "没了", "完了", "就这些", "就这些了", "好了", "全部发完了"}
_INVITE_MAX_CHARS = 1500  # an invite is short; a long text with a meeting number in it is minutes
MEDIA_VALIDITY = timedelta(days=3)  # WeChat media_id lifetime (official doc 90254)


def _with_source():
    """Tasks with the inbox row that created them: messages handled in one pass share `now`, so creation time
    alone cannot tell which of two tasks is newer; the pull order can."""
    t, i = meeting_tasks.c, kf_inbox.c
    return meeting_tasks.outerjoin(
        kf_inbox, (i.open_kfid == t.source_open_kfid) & (i.msg_id == t.source_msg_id)
    )


_NEWEST = (meeting_tasks.c.created_at.desc(), kf_inbox.c.ingest_seq.desc().nulls_last())


def _age_key(row) -> tuple:
    return (row.created_at, row.ingest_seq if row.ingest_seq is not None else -1)


def _is_invite(body: str, parsed: ParsedInvite) -> bool:
    """Short text with a meeting number is a request (the bot asks for what is missing). Longer text must
    also look like an invite (a time, a join link, a password or the invite wording): minutes and
    transcripts mention meeting numbers too."""
    if len(body.strip()) < FORWARDED_MIN_CHARS:
        return True
    if len(body) > _INVITE_MAX_CHARS or body.count("\n") > 30:
        return False
    timed = parsed.starts_at is not None or bool(parsed.flags & _UNUSABLE_TIME)
    return timed or parsed.join_url is not None or parsed.password is not None or "邀请您参加" in body


def _looks_like_followup(body: str, now: datetime) -> bool:
    starts, _, _ = parse_time_range(body, now=now)
    return starts is not None or _password_reply(body) is not None or _norm(body) in _NO_PASSWORD


def _fits_one_request(segments: list[dict]) -> bool:
    if len(segments) >= material.MAX_SEGMENTS:
        return False
    try:
        build_user_prompt(segments)
    except TranscriptTooLong:
        return False
    return True


def _fits_text(text_: str) -> bool:
    """Whether forwarded text can be summarized in one request (checked when it arrives)."""
    return len(text_) <= material.MAX_TEXT_CHARS and _fits_one_request(material.parse_transcript(text_))


def _looks_like_size_notice(body: str) -> bool:
    """WeChat replaces a file over the KF limit by a text notice whose wording is not documented."""
    return ("文件" in body or "视频" in body) and any(
        w in body for w in ("过大", "超过", "超出", "大小限制", "太大")
    )


def _password_reply(body: str) -> str | None:
    """Only an explicit "密码 xxxx" or a bare 4-6 digit reply counts; other words never become passwords."""
    body = body.strip()
    if m := re.fullmatch(r"(?:会议|入会)?密码\s*[:：]?\s*([0-9A-Za-z]{4,12})", body):
        return m.group(1)
    if re.fullmatch(r"\d{4,6}", body):
        return body
    return None


def _norm(text_: str) -> str:
    return re.sub(r"[\s。！!.,，]+", "", text_).lower()


# ---------------------------------------------------------------- inbox loop


async def process_inbox(
    engine: AsyncEngine,
    *,
    now: datetime,
    keyring: Keyring,
    corp_id: str,
    media: MediaPeek | None = None,
    limit: int = 50,
) -> int:
    """Apply inbox rows in arrival order (ingest_seq). A user's row is not taken while an earlier row of the
    same user waits for a retry or is held by a live lease, and once a row fails in this pass the user's
    later rows go back unhandled: "同意" must never overtake the invite it answers."""
    i = kf_inbox.c
    earlier = kf_inbox.alias("earlier")
    e = earlier.c
    waiting_before = (
        select(e.id)
        .where(
            e.user_id == i.user_id,
            e.ingest_seq < i.ingest_seq,
            e.status.in_(("PENDING", "PROCESSING")),
            e.next_attempt_at > now,
        )
        .exists()
    )
    async with engine.begin() as conn:
        claimable = (
            select(i.id)
            .where(i.status.in_(("PENDING", "PROCESSING")), i.next_attempt_at <= now, ~waiting_before)
            .order_by(i.ingest_seq)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (
            await conn.execute(
                update(kf_inbox)
                .where(i.id.in_(claimable.scalar_subquery()))
                .values(status="PROCESSING", attempts=i.attempts + 1, next_attempt_at=now + INBOX_LEASE)
                .returning(i.id, i.attempts, i.ingest_seq, i.user_id, i.message_type)
            )
        ).all()
    handled = 0
    failed_users: set[UUID] = set()  # a row failed: the user's later rows wait for its retry
    postponed_users: set[UUID] = set()  # a file waits for the next pass: so do the user's later rows
    loop = asyncio.get_running_loop()
    prefetch_deadline = loop.time() + PREFETCH_BUDGET_S
    for row in sorted(rows, key=lambda r: r.ingest_seq):
        if row.message_type in _FILE_TYPES and media is not None and loop.time() > prefetch_deadline:
            # Downloads must not hold up the scheduler and everyone else's messages: the rest wait a tick.
            if row.user_id is not None:
                postponed_users.add(row.user_id)
            async with engine.begin() as conn:
                await conn.execute(
                    update(kf_inbox)
                    .where(i.id == row.id)
                    .values(status="PENDING", attempts=i.attempts - 1, next_attempt_at=now)
                )
            continue
        if row.user_id is not None and row.user_id in failed_users | postponed_users:
            retry_at = now + INBOX_LEASE if row.user_id in failed_users else now
            async with engine.begin() as conn:  # not attempted: give the attempt back
                await conn.execute(
                    update(kf_inbox)
                    .where(i.id == row.id)
                    .values(status="PENDING", attempts=i.attempts - 1, next_attempt_at=retry_at)
                )
            continue
        try:
            # A file is looked at (network) before the transaction that acts on it.
            prefetched = None
            if row.message_type in _FILE_TYPES and media is not None:
                async with asyncio.timeout(PREFETCH_TIMEOUT_S):
                    prefetched = await _prefetch_row(engine, row.id, keyring, media)
            await in_transaction_with_retry(
                engine,
                lambda conn, rid=row.id, pre=prefetched: _handle_row(
                    conn, rid, now=now, keyring=keyring, corp_id=corp_id, prefetched=pre
                ),
            )
            handled += 1
        except Exception as exc:  # keep the loop alive; the row becomes due again after the lease
            log.error("inbox row %s failed: %s", row.id, type(exc).__name__)
            final = row.attempts >= MAX_INBOX_ATTEMPTS
            if not final and row.user_id is not None:
                failed_users.add(row.user_id)
            async with engine.begin() as conn:
                await conn.execute(
                    update(kf_inbox)
                    .where(i.id == row.id)
                    .values(status="FAILED" if final else "PENDING", last_error=type(exc).__name__)
                )
                if final and not row.message_type.startswith("event:"):
                    await _tell_unhandled(conn, row.id, row.message_type, now=now)  # never leave silence
    return handled


async def _tell_unhandled(conn: AsyncConnection, row_id: UUID, message_type: str, *, now: datetime) -> None:
    """A customer message given up on: say so, and that sending it again may work."""
    i = kf_inbox.c
    row = (
        await conn.execute(select(i.open_kfid, i.msg_id, i.user_id, i.received_at).where(i.id == row_id))
    ).one()
    if row.user_id is None:
        return
    purpose, content = (
        ("FILE_UNREADABLE", msgs.FILE_UNREADABLE)
        if message_type in _FILE_TYPES
        else ("MESSAGE_FAILED", msgs.message_failed(row.received_at))
    )
    await conn.execute(
        pg_insert(outgoing_messages)
        .values(
            user_id=row.user_id,
            open_kfid=row.open_kfid,
            purpose=purpose,
            dedup_key=f"reply:{row.msg_id}:{purpose}",
            content=content,
            next_attempt_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
    )


async def _prefetch_row(engine: AsyncEngine, row_id: UUID, keyring: Keyring, media: MediaPeek) -> Prefetched:
    i = kf_inbox.c
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(i.open_kfid, i.msg_id, i.message_type, i.content_ciphertext).where(i.id == row_id)
            )
        ).one()
    content = open_content(row.content_ciphertext, row.open_kfid, row.msg_id, keyring)
    return await prefetch(media, content.get(row.message_type, {}).get("media_id", ""))


async def _handle_row(
    conn: AsyncConnection,
    row_id: UUID,
    *,
    now: datetime,
    keyring: Keyring,
    corp_id: str,
    prefetched: Prefetched | None = None,
) -> None:
    i = kf_inbox.c
    row = (
        await conn.execute(
            select(i.open_kfid, i.msg_id, i.user_id, i.message_type, i.content_ciphertext, i.status)
            .where(i.id == row_id)
            .with_for_update()
        )
    ).one()
    if row.status != "PROCESSING":
        return
    content = (
        open_content(row.content_ciphertext, row.open_kfid, row.msg_id, keyring)
        if row.content_ciphertext
        else {}
    )
    if row.user_id is not None:
        await conn.execute(select(kf_users.c.id).where(kf_users.c.id == row.user_id).with_for_update())

    ctx = _Ctx(conn, now, keyring, corp_id, row.open_kfid, row.msg_id, row.user_id)
    match row.message_type:
        case "text":
            await ctx.on_text(content.get("text", {}).get("content", ""))
        case "miniprogram":
            card = content.get("miniprogram", {})
            await ctx.on_invite(parse_miniprogram(card.get("pagepath", ""), card.get("title", "")))
        case "link":
            link = content.get("link", {})
            if material.is_recording_share_link(link.get("url", "")):
                await ctx.reply("SHARE_LINK_HINT", msgs.SHARE_LINK_HINT)
            else:
                await ctx.on_invite(parse_text(f"{link.get('title', '')}\n{link.get('url', '')}", now=now))
        case "file" | "video":
            media_id = content.get(row.message_type, {}).get("media_id", "")
            await ctx.on_file(row.message_type, media_id, prefetched)
        case "voice":
            await ctx.reply("VOICE_NOT_SUPPORTED", msgs.VOICE_NOT_SUPPORTED)
        case kind if not kind.startswith("event:"):
            await ctx.reply("UNSUPPORTED_MESSAGE", msgs.UNSUPPORTED_MESSAGE)  # image, chat record, card ...
        case "event:msg_send_fail":
            await _on_send_fail(conn, content.get("event", {}))
        case _:
            pass  # enter_session welcome is sent from the callback path; other types need no action
    await conn.execute(update(kf_inbox).where(i.id == row_id).values(status="DONE", processed_at=now))


async def _on_send_fail(conn: AsyncConnection, event: dict[str, Any]) -> None:
    """fail_type 4 (session expired) and 6 (over 5 messages) wait for the user's next message."""
    o = outgoing_messages.c
    fail_msgid = event.get("fail_msgid")
    if not (isinstance(fail_msgid, str) and fail_msgid):
        return  # `wecom_msgid == None` would match every message not yet sent
    fail_type = int(event.get("fail_type", 0))
    status = "DEFERRED" if fail_type in (4, 6) else "FAILED"
    changed = await conn.execute(
        update(outgoing_messages)
        .where(o.wecom_msgid == fail_msgid)
        .values(status=status, last_error=f"msg_send_fail type {fail_type}")
        .returning(o.id, o.purpose)
    )
    if status == "FAILED":
        for row in changed:
            log.error(
                "outgoing message %s (%s) failed: msg_send_fail type %d", row.id, row.purpose, fail_type
            )


# ---------------------------------------------------------------- conversation


class _Ctx:
    def __init__(self, conn, now, keyring, corp_id, open_kfid, msg_id, user_id):
        self.conn: AsyncConnection = conn
        self.now: datetime = now
        self.keyring: Keyring = keyring
        self.corp_id: str = corp_id
        self.open_kfid: str = open_kfid
        self.msg_id: str = msg_id
        self.user_id: UUID | None = user_id

    async def reply(self, purpose: str, content: str, task_id: UUID | None = None) -> None:
        if self.user_id is None:
            return
        await self.conn.execute(
            pg_insert(outgoing_messages)
            .values(
                task_id=task_id,
                user_id=self.user_id,
                open_kfid=self.open_kfid,
                purpose=purpose,
                dedup_key=f"reply:{self.msg_id}:{purpose}",
                content=content,
                next_attempt_at=self.now,
            )
            .on_conflict_do_nothing(index_elements=["dedup_key"])
        )

    async def trigger(self, task_id: UUID, trigger: Trigger) -> Transition | None:
        try:
            return await apply_and_settle(
                self.conn, task_id, trigger, now=self.now, actor=f"user:{self.user_id}"
            )
        except InvalidTransition:
            return None

    async def _latest(
        self, statuses, *, not_over: bool = False, kind: TaskKind | None = None
    ) -> tuple[UUID, str] | None:
        """The user's newest task in `statuses`; with not_over, only one whose meeting has not ended."""
        t = meeting_tasks.c
        query = (
            select(t.id, t.status)
            .select_from(_with_source())
            .where(t.user_id == self.user_id, t.status.in_(tuple(statuses)))
        )
        if kind is not None:
            query = query.where(t.task_kind == kind)
        if not_over:
            query = query.where(or_(t.planned_end_at.is_(None), t.planned_end_at > self.now))
        row = (await self.conn.execute(query.order_by(*_NEWEST).limit(1))).one_or_none()
        return (row.id, row.status) if row else None

    # ------------------------------------------------------------ dispatch on text

    async def on_text(self, body: str) -> None:
        word = _norm(body)
        await self._flush_deferred()
        if word in _AGREE:
            return await self.on_agree()
        if word in _CANCEL:
            return await self.on_cancel()
        if word in _STOP:
            return await self.on_stop()
        if word in _DELETE:
            return await self.on_delete()
        if word in _VIEW:
            return await self.on_view()
        if word in _UPLOAD:
            return await self.on_upload_request()
        short = len(body.strip()) < FORWARDED_MIN_CHARS
        if short and material.is_recording_share_link(body):
            return await self.reply("SHARE_LINK_HINT", msgs.SHARE_LINK_HINT)
        parsed = parse_text(body, now=self.now)
        if (parsed.meeting_code or InviteFlag.MULTIPLE_MEETINGS in parsed.flags) and _is_invite(body, parsed):
            return await self.on_invite(parsed)
        appending = await self._open_forwarded()
        if (
            appending is not None
            and short
            and _looks_like_followup(body, self.now)
            and await self.on_followup(body)
        ):
            return  # a time or password for a meeting request, not a line of the minutes
        if appending is not None:  # minutes are still arriving: everything else is their continuation
            if word in _DONE_SENDING:
                return await self.reply("FORWARDED_DONE", msgs.FORWARDED_DONE, appending)
            return await self.on_forwarded_text(body.strip())
        if short and await self.on_followup(body):
            return
        if short and _looks_like_size_notice(body):
            return await self.on_upload_request()  # WeChat's notice for a file over the 20 MB KF limit
        if not short:
            return await self.on_forwarded_text(body.strip())
        await self.reply("NOT_UNDERSTOOD", msgs.NOT_UNDERSTOOD)

    async def _flush_deferred(self) -> None:
        """A new customer message reopens the reply window: resend the newest deferred notice per task."""
        if self.user_id is None:
            return
        o = outgoing_messages.c
        newest = (
            select(func.max(o.created_at))
            .where(o.user_id == self.user_id, o.status == "DEFERRED")
            .group_by(o.task_id)
        )
        await self.conn.execute(
            update(outgoing_messages)
            .where(o.user_id == self.user_id, o.status == "DEFERRED", o.created_at.in_(newest))
            .values(next_attempt_at=self.now, **release_deferred_values())
        )

    # ------------------------------------------------------------ invites

    async def on_invite(self, parsed: ParsedInvite) -> None:
        if InviteFlag.MULTIPLE_MEETINGS in parsed.flags:
            return await self.reply("MULTIPLE_MEETINGS", msgs.MULTIPLE_MEETINGS)
        if InviteFlag.INVITE_ONLY in parsed.flags:
            return await self.reply("INVITE_ONLY", msgs.INVITE_ONLY)
        if InviteFlag.REGISTRATION_REQUIRED in parsed.flags:
            return await self.reply("REGISTRATION_REQUIRED", msgs.REGISTRATION_REQUIRED)
        if InviteFlag.CODE_CONFLICT in parsed.flags:
            return await self.reply("CODE_CONFLICT", msgs.CODE_CONFLICT)
        if parsed.meeting_code is None or self.user_id is None:
            return await self.reply("NOT_UNDERSTOOD", msgs.NOT_UNDERSTOOD)
        if _UNUSABLE_TIME & parsed.flags:
            parsed.starts_at = parsed.ends_at = None  # ask instead of scheduling a wrong or past time
        if InviteFlag.PASSWORD_CONFLICT in parsed.flags:
            parsed.password = None  # two different passwords: ask

        task_id = uuid.uuid4()
        values: dict[str, Any] = {
            "id": task_id,
            "user_id": self.user_id,
            "task_kind": TaskKind.LIVE_MEETING,
            "source_open_kfid": self.open_kfid,
            "source_msg_id": self.msg_id,
            "idempotency_key": f"kf:{self.open_kfid}:{self.msg_id}",
            "created_at": self.now,  # the clock the intake expiry and link lifetimes are measured with
            "meeting_code": parsed.meeting_code,
            "scheduled_at": parsed.starts_at,
            "planned_end_at": parsed.ends_at
            or (parsed.starts_at + DEFAULT_DURATION if parsed.starts_at else None),
        }
        values.update(self._seal_credentials(task_id, parsed.join_url, parsed.password))
        created = await self.conn.execute(
            pg_insert(meeting_tasks)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["user_id", "idempotency_key"])
            .returning(meeting_tasks.c.id)
        )
        if created.first() is None:
            return  # this message already created its task
        await self._advance(task_id, subject=parsed.subject, password_known=parsed.password is not None)

    def _seal_credentials(self, task_id: UUID, join_url: str | None, password: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for column, value in (("join_link_ciphertext", join_url), ("meeting_password_ciphertext", password)):
            if value:
                sealed = self.keyring.encrypt_text(value, aad=credential_aad(task_id, column))
                out[column] = sealed.ciphertext
                out["secret_key_version"] = sealed.key_version
        return out

    async def _advance(self, task_id: UUID, *, subject: str | None, password_known: bool) -> None:
        """AWAITING_DETAILS -> AWAITING_CONSENT once code and time are known; otherwise ask."""
        t = meeting_tasks.c
        row = (
            await self.conn.execute(
                select(t.status, t.meeting_code, t.scheduled_at, t.planned_end_at).where(t.id == task_id)
            )
        ).one()
        if row.meeting_code is None:
            return  # not a meeting request (defensive: follow-ups only reach meeting requests)
        if row.scheduled_at is None:
            return await self.reply("ASK_TIME", msgs.ASK_TIME, task_id)
        if row.status == TaskStatus.AWAITING_DETAILS:
            await self.trigger(task_id, Trigger(TriggerType.DETAILS_COMPLETE))
        text_ = msgs.confirm_meeting(row.meeting_code, row.scheduled_at, row.planned_end_at, subject)
        if not password_known:
            text_ += "\n\n" + msgs.ASK_PASSWORD
        await self.reply("CONFIRM", text_, task_id)

    async def on_followup(self, body: str) -> bool:
        """A reply to ASK_TIME or ASK_PASSWORD for the user's open meeting request (never a fallback task:
        uploads and forwarded text have no meeting time or password)."""
        latest = await self._latest(
            (TaskStatus.AWAITING_DETAILS, TaskStatus.AWAITING_CONSENT),
            not_over=True,
            kind=TaskKind.LIVE_MEETING,
        )
        if latest is None:
            return False
        task_id, status = latest
        starts, ends, flags = parse_time_range(body, now=self.now)
        if starts is not None and _UNUSABLE_TIME & flags:
            await self.reply("ASK_TIME", msgs.ASK_TIME, task_id)
            return True
        if starts is not None and status == TaskStatus.AWAITING_DETAILS:
            await self.conn.execute(
                update(meeting_tasks)
                .where(meeting_tasks.c.id == task_id)
                .values(scheduled_at=starts, planned_end_at=ends or starts + DEFAULT_DURATION)
            )
            await self._advance(task_id, subject=None, password_known=True)
            return True
        word = _norm(body)
        if word in _NO_PASSWORD:
            await self.reply("PASSWORD_NOTED", "好的，记为无密码会议。确认无误请回复「同意」。", task_id)
            return True
        password = _password_reply(body)
        if password:
            await self.conn.execute(
                update(meeting_tasks)
                .where(meeting_tasks.c.id == task_id)
                .values(**self._seal_credentials(task_id, None, password))
            )
            await self.reply("PASSWORD_NOTED", "密码已记录。确认无误请回复「同意」。", task_id)
            return True
        return False

    # ------------------------------------------------------------ commands

    async def on_agree(self) -> None:
        """'同意' answers the newest confirmation the user was asked for. A meeting request that is over (or
        that the scheduler already dropped) gets MEETING_OVER; it never falls through to an older task."""
        t, i, o = meeting_tasks.c, kf_inbox.c, outgoing_messages.c
        pending = (
            await self.conn.execute(
                select(t.id, t.task_kind, t.planned_end_at, t.created_at, i.ingest_seq)
                .select_from(_with_source())
                .where(t.user_id == self.user_id, t.status == TaskStatus.AWAITING_CONSENT)
                .order_by(*_NEWEST)
                .limit(1)
            )
        ).one_or_none()
        dropped = (
            await self.conn.execute(
                select(t.id, t.created_at, i.ingest_seq)
                .select_from(_with_source())
                .where(
                    t.user_id == self.user_id,
                    t.task_kind == TaskKind.LIVE_MEETING,
                    t.status == TaskStatus.CANCELED,
                    t.consent_at.is_(None),
                    t.cancel_requested_at.is_(None),  # INTAKE_EXPIRED, not the user's own cancel
                    t.planned_end_at > self.now - INTAKE_MEMORY,
                    ~select(o.id)
                    .where(o.task_id == t.id, o.purpose == "MEETING_OVER")
                    .exists(),  # already explained once: it must not block later confirmations
                )
                .order_by(*_NEWEST)
                .limit(1)
            )
        ).one_or_none()
        if dropped is not None and (pending is None or _age_key(dropped) > _age_key(pending)):
            return await self.reply("MEETING_OVER", msgs.MEETING_OVER, dropped.id)
        if pending is None:
            return await self.reply("NOT_UNDERSTOOD", msgs.NOT_UNDERSTOOD)
        task_id = pending.id
        if pending.task_kind != TaskKind.LIVE_MEETING:
            return await self._agree_fallback()
        if pending.planned_end_at is not None and pending.planned_end_at <= self.now:
            await self.trigger(task_id, Trigger(TriggerType.INTAKE_EXPIRED))
            return await self.reply("MEETING_OVER", msgs.MEETING_OVER, task_id)
        # The user's own recordings and texts awaiting confirmation go along (they were announced with
        # their own confirmation requests); the meeting gets its SCHEDULED notice.
        await self._agree_fallback(meeting_follows=True)
        if not await self._has_capacity(task_id):
            return await self.reply("NO_CAPACITY", msgs.NO_CAPACITY, task_id)
        if await self.trigger(
            task_id, Trigger(TriggerType.CONSENT_GRANTED, consent_version=msgs.CONSENT_VERSION)
        ):
            await self._record_consent(task_id, msgs.CONSENT_VERSION, msgs.CONSENT_TEXT_SHA256)

    async def _agree_fallback(self, *, meeting_follows: bool = False) -> None:
        """Every recording and text the user handed in and has not confirmed yet: each was announced with
        its own confirmation request, and one '同意' answers them all (each gets its own consent record)."""
        t = meeting_tasks.c
        waiting = (
            await self.conn.execute(
                select(t.id, t.task_kind)
                .where(
                    t.user_id == self.user_id,
                    t.status == TaskStatus.AWAITING_CONSENT,
                    t.task_kind.in_((TaskKind.USER_UPLOAD, TaskKind.FORWARDED_MINUTES)),
                )
                .order_by(t.created_at)
            )
        ).all()
        started = 0
        for task_id, kind in waiting:
            if kind == TaskKind.USER_UPLOAD:
                started += await self._agree_upload(task_id)
            else:
                started += await self._agree_forwarded(task_id)
        if started:
            text_ = msgs.PROCESSING if started == 1 else msgs.PROCESSING_MANY.format(count=started)
            waiting_meeting = await self._latest(
                (TaskStatus.AWAITING_CONSENT,), not_over=True, kind=TaskKind.LIVE_MEETING
            )
            if waiting_meeting and not meeting_follows:
                text_ += msgs.MEETING_STILL_WAITING
            await self.reply("PROCESSING", text_, waiting[-1][0])

    async def _agree_upload(self, task_id: UUID) -> bool:
        consent = Trigger(TriggerType.CONSENT_GRANTED, consent_version=msgs.UPLOAD_CONSENT_VERSION)
        if not await self.trigger(task_id, consent):
            return False
        await self._record_consent(task_id, msgs.UPLOAD_CONSENT_VERSION, msgs.UPLOAD_CONSENT_SHA256)
        return True

    async def _agree_forwarded(self, task_id: UUID) -> bool:
        """The forwarded text becomes the task's transcript, then minutes are summarized from it."""
        source = await read_input(self.conn, self.keyring, task_id, lock=True)
        segments = material.parse_transcript(source.payload) if source and source.payload else []
        if not segments or not _fits_one_request(segments):
            await self.trigger(task_id, Trigger(TriggerType.UPLOAD_INVALID))
            return False
        consent = Trigger(TriggerType.CONSENT_GRANTED, consent_version=msgs.FORWARDED_CONSENT_VERSION)
        if not await self.trigger(task_id, consent):
            return False
        await self.conn.execute(
            insert(transcripts).values(
                task_id=task_id,
                source="forwarded_text",
                body={"segments": segments},
                language="zh",
                version=1,
            )
        )
        await consume_input(self.conn, task_id, now=self.now)
        await self._record_consent(task_id, msgs.FORWARDED_CONSENT_VERSION, msgs.FORWARDED_CONSENT_SHA256)
        return True

    async def _record_consent(self, task_id: UUID, version: str, text_sha256: str) -> None:
        await record_requester_consent(
            self.conn,
            task_id=task_id,
            user_id=self.user_id,
            channel="wecom_kf",
            evidence_ref=self.msg_id,
            version=version,
            text_sha256=text_sha256,
            now=self.now,
        )

    # ------------------------------------------------------------ fallback path: recordings and texts

    async def _new_task(self, kind: TaskKind) -> UUID | None:
        """A fallback task created by this message, already past AWAITING_DETAILS; None if this message
        already created one (a redelivery)."""
        task_id = uuid.uuid4()
        created = await self.conn.execute(
            pg_insert(meeting_tasks)
            .values(
                id=task_id,
                user_id=self.user_id,
                task_kind=kind,
                source_open_kfid=self.open_kfid,
                source_msg_id=self.msg_id,
                idempotency_key=f"kf:{self.open_kfid}:{self.msg_id}",
                created_at=self.now,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "idempotency_key"])
            .returning(meeting_tasks.c.id)
        )
        if created.first() is None:
            return None
        await self.trigger(task_id, Trigger(TriggerType.DETAILS_COMPLETE))
        return task_id

    async def on_file(self, msg_type: str, media_id: str, prefetched: Prefetched | None) -> None:
        if self.user_id is None or not media_id:
            return
        # Without a way to look (WeChat media not configured) a file is taken for a recording.
        found = prefetched or Prefetched(material.Kind.MEDIA)
        if found.kind is None:
            return await self.reply("FILE_EXPIRED", msgs.FILE_EXPIRED)
        if found.kind == material.Kind.PDF:
            return await self.reply("PDF_NOT_SUPPORTED", msgs.PDF_NOT_SUPPORTED)
        if found.kind == material.Kind.LEGACY_DOC:
            return await self.reply("LEGACY_DOC", msgs.LEGACY_DOC)
        if found.kind == material.Kind.DOCUMENT and found.text:
            return await self.on_forwarded_text(found.text, file_name=found.file_name)
        if found.kind != material.Kind.MEDIA:
            return await self.reply("UNSUPPORTED_FILE", msgs.UNSUPPORTED_FILE)
        task_id = await self._new_task(TaskKind.USER_UPLOAD)
        if task_id is None:
            return
        await put_input(
            self.conn,
            self.keyring,
            task_id=task_id,
            kind=InputKind.WECOM_MEDIA,
            payload=media_id,
            source_msg_type=msg_type,
            file_name=found.file_name,
            expires_at=self.now + MEDIA_VALIDITY,
        )
        await self.reply("UPLOAD_CONFIRM", msgs.UPLOAD_CONFIRM, task_id)

    async def _open_forwarded(self) -> UUID | None:
        """The user's unconfirmed forwarded text that is still open for continuation."""
        t, i = meeting_tasks.c, task_inputs.c
        return await self.conn.scalar(
            select(t.id)
            .join(task_inputs, i.task_id == t.id)
            .where(
                t.user_id == self.user_id,
                t.task_kind == TaskKind.FORWARDED_MINUTES,
                t.status == TaskStatus.AWAITING_CONSENT,
                i.kind == InputKind.FORWARDED_TEXT,
                i.expires_at > self.now,  # for forwarded text: the end of the append window
            )
            .order_by(t.created_at.desc())
            .limit(1)
            .with_for_update(of=meeting_tasks)
        )

    async def on_forwarded_text(self, body: str, *, file_name: str | None = None) -> None:
        """Copied or exported minutes / transcript. Text sent within APPEND_WINDOW of an unconfirmed one is
        its continuation (long transcripts arrive in several messages). Text that cannot fit one minutes
        request is refused here, before any consent, not after."""
        if self.user_id is None:
            return
        open_task = None if file_name else await self._open_forwarded()
        if open_task is not None:
            source = await read_input(self.conn, self.keyring, open_task, lock=True)
            if source and source.payload is not None:
                combined = f"{source.payload}\n{body}"
                if not _fits_text(combined):
                    return await self.reply("TEXT_TOO_LONG", msgs.TEXT_TOO_LONG, open_task)
                await replace_payload(
                    self.conn, self.keyring, open_task, combined, expires_at=self.now + APPEND_WINDOW
                )
                return await self.reply(
                    "FORWARDED_APPENDED", msgs.FORWARDED_APPENDED.format(chars=len(combined)), open_task
                )
        if not _fits_text(body):
            return await self.reply("TEXT_TOO_LONG", msgs.TEXT_TOO_LONG)
        task_id = await self._new_task(TaskKind.FORWARDED_MINUTES)
        if task_id is None:
            return
        await put_input(
            self.conn,
            self.keyring,
            task_id=task_id,
            kind=InputKind.FORWARDED_TEXT,
            payload=body,
            source_msg_type="file" if file_name else "text",
            file_name=file_name,
            expires_at=None if file_name else self.now + APPEND_WINDOW,
        )
        confirm = msgs.FORWARDED_FILE_CONFIRM if file_name else msgs.FORWARDED_CONFIRM
        await self.reply("FORWARDED_CONFIRM", confirm.format(chars=len(body)), task_id)

    async def on_upload_request(self) -> None:
        """A one-time upload link (minted when the reply is sent) for a recording too large for WeChat."""
        if self.user_id is None:
            return
        t = meeting_tasks.c
        task_id = await self.conn.scalar(
            select(t.id)
            .where(
                t.user_id == self.user_id,
                t.task_kind == TaskKind.USER_UPLOAD,
                t.status == TaskStatus.AWAITING_DETAILS,
                # a reused task must outlive the new link (INTAKE_EXPIRED drops it 48 h after creation)
                t.created_at > self.now - (INTAKE_TTL - LINK_TTL),
            )
            .order_by(t.created_at.desc())
            .limit(1)
        )
        if task_id is None:
            task_id = uuid.uuid4()
            await self.conn.execute(
                insert(meeting_tasks).values(
                    id=task_id,
                    user_id=self.user_id,
                    task_kind=TaskKind.USER_UPLOAD,
                    source_open_kfid=self.open_kfid,
                    source_msg_id=self.msg_id,
                    idempotency_key=f"kf:{self.open_kfid}:{self.msg_id}",
                    created_at=self.now,
                )
            )
        await self.reply("UPLOAD_LINK", msgs.UPLOAD_LINK, task_id)

    async def _has_capacity(self, task_id: UUID) -> bool:
        """Occupancy [scheduled_at - lead, planned_end_at + 30 min] against available Workers (§5.4)."""
        await self.conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('schedule_capacity'))"))
        t, w = meeting_tasks.c, worker_nodes.c
        window = (
            await self.conn.execute(select(t.scheduled_at, t.planned_end_at).where(t.id == task_id))
        ).one()
        start, end = window.scheduled_at - OCCUPANCY_BEFORE, window.planned_end_at + OCCUPANCY_AFTER
        overlapping = await self.conn.scalar(
            select(func.count()).where(
                t.id != task_id,
                t.status.in_(_OCCUPYING),
                t.scheduled_at - literal_column(f"interval '{int(OCCUPANCY_BEFORE.total_seconds())} seconds'")
                < end,
                t.planned_end_at
                + literal_column(f"interval '{int(OCCUPANCY_AFTER.total_seconds())} seconds'")
                > start,
            )
        )
        workers = await self.conn.scalar(
            select(func.count()).where(w.status != "DISABLED", w.revoked_at.is_(None))
        )
        return overlapping < workers

    async def on_cancel(self) -> None:
        active = set(TaskStatus) - TERMINAL_STATUSES
        latest = await self._latest(active)
        if latest is None or not await self.trigger(latest[0], Trigger(TriggerType.CANCEL_REQUESTED)):
            await self.reply("NOTHING_TO_CANCEL", msgs.NOTHING_TO_CANCEL)

    async def on_stop(self) -> None:
        latest = await self._latest(IN_ATTEMPT_STATUSES | {TaskStatus.RETRY_WAIT, TaskStatus.DISPATCHING})
        transition = latest and await self.trigger(latest[0], Trigger(TriggerType.STOP_REQUESTED))
        if not transition:
            return await self.reply("NOTHING_TO_STOP", msgs.NOTHING_TO_STOP)
        if transition.to_status == TaskStatus.UPLOADING:
            await self.reply("STOPPED", msgs.STOPPED, latest[0])
        # Nothing recorded yet: stopping cancelled the task, and its CANCELED notice says so.

    async def on_view(self) -> None:
        """A fresh one-time link to the newest minutes (minted when the reply is sent)."""
        t = meeting_tasks.c
        task_id = await self.conn.scalar(
            select(t.id)
            .where(
                t.user_id == self.user_id,
                t.status == TaskStatus.COMPLETED,
                t.content_purged_at.is_(None),
                or_(t.retention_until.is_(None), t.retention_until > self.now),
            )
            .order_by(t.created_at.desc())
            .limit(1)
        )
        if task_id is None:
            return await self.reply("NO_RESULT", msgs.NO_RESULT)
        await self.reply("RESULT_LINK", msgs.RESULT_LINK, task_id)

    async def on_delete(self) -> None:
        await self.conn.execute(
            update(kf_users)
            .where(kf_users.c.id == self.user_id, or_(kf_users.c.deletion_requested_at.is_(None)))
            .values(deletion_requested_at=self.now)
        )
        await self.reply("DELETE_REQUESTED", "已收到删除请求，你的会议数据将在 24 小时内删除。")
