"""Parse Tencent Meeting invitations sent to the WeChat KF account (design doc §1, module 2).

Deterministic rules first: the "复制邀请" text, mini-program cards and links. Anything the rules cannot
settle is flagged (or left empty, so the conversation asks) rather than guessed; every result is echoed
back to the user for confirmation before a task is scheduled.
"""

import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from enum import StrEnum
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("Asia/Shanghai")

# Official docs give 9-12 digit codes; written either contiguously or as three groups of 3-4 digits.
_CODE = r"(\d{9,12}|\d{3,4}(?:[ \t-]\d{3,4}){2})(?!\d)"
_CODE_LINE = re.compile(rf"#?[ \t]*(?:腾讯会议|会议号|VooV Meeting|Meeting ID)[ \t]*[:：][ \t]*{_CODE}", re.I)
_PASSWORD_LINE = re.compile(
    r"(?:会议密码|入会密码|Passcode|Password)[ \t]*[:：][ \t]*([0-9A-Za-z]{4,12})(?![0-9A-Za-z])", re.I
)
_SUBJECT_LINE = re.compile(r"(?:会议主题|Meeting Topic|Topic)[ \t]*[:：][ \t]*(.+)", re.I)
_TIME_LINE = re.compile(r"(?:会议时间|Time)[ \t]*[:：][ \t]*(.+)", re.I)
# One-tap dial line: +8675536550000,,<code>#,,#,,<password>#
_DIAL_LINE = re.compile(r"\+\d{6,15},,(\d{9,12})#(?:,,#)?(?:,,([0-9A-Za-z]{4,12})#)?")
# RFC 3986 characters only: Chinese text or punctuation right after a link is never part of it.
_URL = re.compile(r"(?:https?|wemeet)://[A-Za-z0-9\-._~:/?#\[\]@!$&*+=%]+", re.I)
_MEETING_HOSTS = ("meeting.tencent.com", "wemeet.qq.com", "voovmeeting.com")

_DATE = r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})"
_HM = r"(\d{1,2}):(\d{2})"
_TZ_MARK = re.compile(r"(?:GMT|UTC)\s*(?:([+-])\s*(\d{1,2})(?::?(\d{2}))?)?", re.I)
_MERIDIEM = re.compile(r"\b(?:AM|PM)\b|上午|下午|晚上|中午|凌晨|早上", re.I)
_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


class InviteFlag(StrEnum):
    """Facts the conversation layer must act on (ask, warn or refuse)."""

    MISSING_CODE = "MISSING_CODE"
    MISSING_TIME = "MISSING_TIME"
    MISSING_PASSWORD_UNKNOWN = "MISSING_PASSWORD_UNKNOWN"  # no password line; ask if the meeting has one
    MULTIPLE_MEETINGS = "MULTIPLE_MEETINGS"
    CODE_CONFLICT = "CODE_CONFLICT"  # code line, dial line and links disagree
    PASSWORD_CONFLICT = "PASSWORD_CONFLICT"
    RECURRING = "RECURRING"  # periodic meetings usually sit behind a waiting room
    WEBINAR = "WEBINAR"
    REGISTRATION_REQUIRED = "REGISTRATION_REQUIRED"  # the bot cannot register
    INVITE_ONLY = "INVITE_ONLY"  # 特邀会议: nobody else can join
    ENDS_BEFORE_START = "ENDS_BEFORE_START"
    STARTS_IN_PAST = "STARTS_IN_PAST"
    TIME_UNCLEAR = "TIME_UNCLEAR"  # AM/PM words, unknown zone or recurrence rule: ask instead of guessing


@dataclass
class ParsedInvite:
    meeting_code: str | None = None  # digits only
    password: str | None = None
    join_url: str | None = None
    subject: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    flags: set[InviteFlag] = field(default_factory=set)
    needs_llm: bool = False  # the rules found nothing usable but the message may still be a request

    @property
    def complete(self) -> bool:
        return self.meeting_code is not None and self.starts_at is not None


def normalize(text: str) -> str:
    """Full-width digits, colons and spaces become ASCII; line endings are unified."""
    return unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def is_meeting_url(url: str) -> bool:
    """Exact Tencent hosts or their subdomains; no userinfo, no backslashes (browsers read them as '/')."""
    if "\\" in url:
        return False
    if url.lower().startswith("wemeet://"):
        return True
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return False
    host = (parts.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _MEETING_HOSTS)


def parse_text(text: str, *, now: datetime | None = None, tz: ZoneInfo = DEFAULT_TZ) -> ParsedInvite:
    text = normalize(text)
    now = now or datetime.now(tz)
    result = ParsedInvite()

    line_codes = {_digits(m.group(1)) for m in _CODE_LINE.finditer(text)}
    dial = [(m.group(1), m.group(2)) for m in _DIAL_LINE.finditer(text)]
    dial_codes = {c for c, _ in dial}
    urls = [u.rstrip(".:;!?") for u in _URL.findall(text)]
    meeting_urls = [u for u in urls if is_meeting_url(u)]
    url_codes = {c for c in (code_from_url(u) for u in meeting_urls) if c}

    primary = line_codes or dial_codes or url_codes
    if len(primary) > 1 or len(url_codes) > 1:
        result.flags.add(InviteFlag.MULTIPLE_MEETINGS)
    if primary:
        result.meeting_code = sorted(primary)[0]
        if (dial_codes or url_codes) - {result.meeting_code}:
            result.flags.add(InviteFlag.CODE_CONFLICT)
    for url in meeting_urls:
        code = code_from_url(url)
        if code is None or code == result.meeting_code:
            result.join_url = url  # never hand the Worker a link to a different meeting
            break

    passwords = {m.group(1) for m in _PASSWORD_LINE.finditer(text)}
    dial_passwords = {p for _, p in dial if p}
    if passwords:
        result.password = sorted(passwords)[0]
        if len(passwords) > 1 or (dial_passwords and not dial_passwords & passwords):
            result.flags.add(InviteFlag.PASSWORD_CONFLICT)
    elif dial_passwords:
        result.password = sorted(dial_passwords)[0]
        if len(dial_passwords) > 1:
            result.flags.add(InviteFlag.PASSWORD_CONFLICT)

    if m := _SUBJECT_LINE.search(text):
        result.subject = m.group(1).strip()[:200]

    if m := _TIME_LINE.search(text):
        recurrence = text[m.end() : m.end() + 200]
        starts, ends, flags = parse_time_range(m.group(1), now=now, tz=tz, following=recurrence)
        result.starts_at, result.ends_at = starts, ends
        result.flags |= flags

    if "网络研讨会" in text or re.search(r"\bwebinar\b", text, re.I):
        result.flags.add(InviteFlag.WEBINAR)
    if "报名" in text or re.search(r"\bregist(er|ration)\b", text, re.I):
        result.flags.add(InviteFlag.REGISTRATION_REQUIRED)
    if "特邀会议" in text:
        result.flags.add(InviteFlag.INVITE_ONLY)

    if result.meeting_code is None:
        result.flags.add(InviteFlag.MISSING_CODE)
        result.needs_llm = bool(re.search(r"会议|meeting|\d{3}[ \t-]\d{3}[ \t-]\d{3}", text, re.I))
    if result.starts_at is None:
        result.flags.add(InviteFlag.MISSING_TIME)
    if result.password is None and result.meeting_code is not None:
        result.flags.add(InviteFlag.MISSING_PASSWORD_UNKNOWN)
    return result


def parse_time_range(
    value: str, *, now: datetime | None = None, tz: ZoneInfo = DEFAULT_TZ, following: str = ""
) -> tuple[datetime | None, datetime | None, set[InviteFlag]]:
    """Parse a 会议时间 value: "2026/10/10 15:00-16:00", cross-day "2026/10/10 23:30-2026/10/11 00:30" and
    recurring "2026/10/10-2026/11/10 15:00-16:00" followed by a rule such as "每周 (周五)" in `value` or
    `following`. Recurring meetings resolve to the next occurrence not before `now`. Anything ambiguous
    (AM/PM words, an unparseable zone, an unknown rule) returns no time plus TIME_UNCLEAR."""
    value = normalize(value).split("\n")[0]
    now = now or datetime.now(tz)
    flags: set[InviteFlag] = set()
    if _MERIDIEM.search(value):
        return None, None, {InviteFlag.TIME_UNCLEAR}

    zone: timezone | ZoneInfo = tz
    if mark := _TZ_MARK.search(value):
        if mark.group(1) is None:
            zone = UTC
        else:
            hours, minutes = int(mark.group(2)), int(mark.group(3) or 0)
            if hours > 14 or minutes >= 60:
                return None, None, {InviteFlag.TIME_UNCLEAR}
            sign = 1 if mark.group(1) == "+" else -1
            zone = timezone(sign * timedelta(hours=hours, minutes=minutes))

    if m := re.search(rf"{_DATE}\s+{_HM}\s*-\s*{_DATE}\s+{_HM}", value):
        y1, mo1, d1, h1, mi1, y2, mo2, d2, h2, mi2 = map(int, m.groups())
        start, end = _dt(y1, mo1, d1, h1, mi1, zone), _dt(y2, mo2, d2, h2, mi2, zone)
    elif m := re.search(rf"{_DATE}\s*-\s*{_DATE}\D+?{_HM}\s*-\s*{_HM}", value):
        y, mo, d, y2, mo2, d2, h1, mi1, h2, mi2 = map(int, m.groups())
        flags.add(InviteFlag.RECURRING)
        first, last = _date(y, mo, d), _date(y2, mo2, d2)
        rule = _recurrence(value + "\n" + normalize(following))
        if first is None or last is None or rule is None:
            return None, None, flags | {InviteFlag.TIME_UNCLEAR}
        day = _next_occurrence(rule, first, last, now.astimezone(zone), h2, mi2)
        if day is None:  # the series is over
            return None, None, flags | {InviteFlag.STARTS_IN_PAST}
        start, end = _window(day.year, day.month, day.day, h1, mi1, h2, mi2, zone, flags)
    elif m := re.search(rf"{_DATE}\s+{_HM}\s*-\s*{_HM}", value):
        y, mo, d, h1, mi1, h2, mi2 = map(int, m.groups())
        start, end = _window(y, mo, d, h1, mi1, h2, mi2, zone, flags)
    elif m := re.search(rf"{_DATE}\s+{_HM}", value):
        y, mo, d, h, mi = map(int, m.groups())
        start, end = _dt(y, mo, d, h, mi, zone), None
    else:
        return None, None, flags

    if start and end and end <= start:
        flags.add(InviteFlag.ENDS_BEFORE_START)
    if start and (end or start) < now:
        flags.add(InviteFlag.STARTS_IN_PAST)
    return start, end, flags


def _dt(y: int, mo: int, d: int, h: int, mi: int, zone) -> datetime | None:
    try:
        return datetime(y, mo, d, h, mi, tzinfo=zone)
    except ValueError:
        return None


def _date(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _window(y, mo, d, h1, mi1, h2, mi2, zone, flags) -> tuple[datetime | None, datetime | None]:
    start = _dt(y, mo, d, h1, mi1, zone)
    end = _dt(y, mo, d, h2, mi2, zone)
    if start and end and end <= start:
        if h1 >= 18 and h2 <= 6:  # "23:30-00:30": an evening meeting crossing midnight
            end += timedelta(days=1)
        else:
            flags.add(InviteFlag.ENDS_BEFORE_START)
    return start, end


@dataclass(frozen=True)
class _Rule:
    weekdays: frozenset[int] | None = None  # None = every day
    every_weeks: int = 1
    month_day: int | None = None


def _recurrence(text: str) -> _Rule | None:
    if re.search(r"每(?:个)?工作日", text):
        return _Rule(weekdays=frozenset(range(5)))
    if re.search(r"每天|每日", text):
        return _Rule()
    if m := re.search(r"每(两|2)?周\s*[(（]?\s*((?:周[一二三四五六日天][、,，\s]*)+)", text):
        days = frozenset(_WEEKDAYS[c] for c in re.findall(r"周([一二三四五六日天])", m.group(2)))
        return _Rule(weekdays=days, every_weeks=2 if m.group(1) else 1)
    if m := re.search(r"每月\s*[(（]?\s*(\d{1,2})\s*日", text):
        return _Rule(month_day=int(m.group(1)))
    return None


def _next_occurrence(
    rule: _Rule, first: date, last: date, now: datetime, end_h: int, end_m: int
) -> date | None:
    """First day in [first, last] matching the rule whose meeting has not ended by `now`."""
    day = max(first, now.date())
    if (now.hour, now.minute) >= (end_h, end_m) and day == now.date():
        day += timedelta(days=1)
    first_week = first - timedelta(days=first.weekday())
    while day <= last:
        if rule.month_day is not None:
            ok = day.day == min(rule.month_day, calendar.monthrange(day.year, day.month)[1])
        else:
            ok = rule.weekdays is None or day.weekday() in rule.weekdays
            if ok and rule.every_weeks > 1:
                ok = ((day - timedelta(days=day.weekday()) - first_week).days // 7) % rule.every_weeks == 0
        if ok:
            return day
        day += timedelta(days=1)
    return None


def code_from_url(url: str) -> str | None:
    """Only /p/<code> links and wemeet:// schemes carry the code; /dm/ and /w/ short links do not."""
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    for key in ("meeting_code", "code"):
        if key in query and re.fullmatch(r"\d{9,12}", query[key][0]):
            return query[key][0]
    if m := re.fullmatch(r"/p/(\d{9,12})/?", parsed.path):
        return m.group(1)
    return None


def parse_miniprogram(pagepath: str, title: str = "") -> ParsedInvite:
    """A forwarded Tencent Meeting card. Two pagepath forms are known: `...?code=<code>` and
    `...?scene=m%3D<code>` (the latter from a third-party source; both need live confirmation)."""
    result = ParsedInvite(subject=title.strip()[:200] or None)
    path = unquote(pagepath)
    m = re.search(r"[?&](?:code|meeting_code)=(\d{9,12})(?!\d)", path) or re.search(
        r"[?&]scene=m=(\d{9,12})(?!\d)", path
    )
    if m:
        result.meeting_code = m.group(1)
        result.flags.add(InviteFlag.MISSING_PASSWORD_UNKNOWN)
    else:
        result.flags.add(InviteFlag.MISSING_CODE)
    result.flags.add(InviteFlag.MISSING_TIME)  # cards carry no time; ask the user
    return result
