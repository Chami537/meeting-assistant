"""Invitation parsing. Samples follow the 腾讯会议「复制邀请」template (a 2022 real sample as reference);
current client templates still need confirming with live copies."""

from datetime import datetime, timedelta, timezone

import pytest
from app.intake.invite_parser import (
    InviteFlag,
    code_from_url,
    is_meeting_url,
    parse_miniprogram,
    parse_text,
    parse_time_range,
)

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=CST)  # a Friday

STANDARD = """张三 邀请您参加腾讯会议
会议主题：产品周会
会议时间：2026/10/10 15:00-16:00 (GMT+08:00) 中国标准时间 - 北京

点击链接入会，或添加至会议列表：
https://meeting.tencent.com/dm/AbCdEf123

#腾讯会议：936-109-253
会议密码：1234

手机一键拨号入会
+8675536550000,,936109253#,,#,,1234# (中国大陆)
+8675595804000,,936109253#,,#,,1234# (中国香港)
"""


def p(text: str):
    return parse_text(text, now=NOW)


def test_standard_invitation():
    r = p(STANDARD)
    assert r.meeting_code == "936109253"
    assert r.password == "1234"
    assert r.join_url == "https://meeting.tencent.com/dm/AbCdEf123"
    assert r.subject == "产品周会"
    assert r.starts_at == datetime(2026, 10, 10, 15, 0, tzinfo=CST)
    assert r.ends_at == datetime(2026, 10, 10, 16, 0, tzinfo=CST)
    assert r.flags == set()
    assert r.complete


def test_full_width_characters_are_normalized():
    r = p("#腾讯会议：９３６－１０９－２５３\n会议密码：１２３４")
    assert (r.meeting_code, r.password) == ("936109253", "1234")


def test_code_without_password_asks_about_password():
    r = p("#腾讯会议：936-109-253\n会议时间：2026/10/10 15:00-16:00")
    assert r.password is None
    assert InviteFlag.MISSING_PASSWORD_UNKNOWN in r.flags


@pytest.mark.parametrize(
    "text",
    [
        "#腾讯会议：936-109-253\n1234",
        "会议号：936109253 2026/10/10 15:00-16:00",
        "会议号：936 109 253 10/10 15:00",
        "#腾讯会议：936-109-253 1234",
    ],
)
def test_code_never_swallows_following_digits(text):
    assert p(text).meeting_code == "936109253"


def test_code_label_does_not_reach_into_next_line():
    assert p("会议号：\n2026-10-10 15:00 开会").meeting_code is None


@pytest.mark.parametrize(
    ("text", "code"), [("#腾讯会议：1234-567-890", "1234567890"), ("会议号：936-1092-5333", "93610925333")]
)
def test_longer_codes(text, code):
    assert p(text).meeting_code == code


def test_dial_line_alone_gives_code_and_password():
    r = p("+8675536550000,,936109253#,,#,,1234# (中国大陆)")
    assert (r.meeting_code, r.password) == ("936109253", "1234")


def test_code_conflict_between_code_line_and_dial_line():
    r = p("#腾讯会议：936-109-253\n+8675536550000,,111222333#,,#,,1234#")
    assert InviteFlag.CODE_CONFLICT in r.flags


def test_password_conflict():
    r = p("#腾讯会议：936-109-253\n会议密码：1234\n+8675536550000,,936109253#,,#,,9999#")
    assert InviteFlag.PASSWORD_CONFLICT in r.flags


def test_two_meetings_in_one_message():
    assert InviteFlag.MULTIPLE_MEETINGS in p("#腾讯会议：936-109-253\n#腾讯会议：111-222-333").flags


def test_no_code_but_looks_like_a_meeting_goes_to_llm():
    r = p("明天下午三点的会议帮我记录一下")
    assert r.meeting_code is None
    assert {InviteFlag.MISSING_CODE, InviteFlag.MISSING_TIME} <= r.flags
    assert r.needs_llm


def test_small_talk_is_not_sent_to_llm():
    assert not p("你好").needs_llm


@pytest.mark.parametrize(
    ("text", "flag"),
    [
        ("网络研讨会\n#腾讯会议：936-109-253", InviteFlag.WEBINAR),
        ("请先报名\n#腾讯会议：936-109-253", InviteFlag.REGISTRATION_REQUIRED),
        ("特邀会议\n#腾讯会议：936-109-253", InviteFlag.INVITE_ONLY),
    ],
)
def test_blocking_meeting_kinds_are_flagged(text, flag):
    assert flag in p(text).flags


def test_english_voov_invitation():
    r = p(
        "Zhang San is inviting you to a VooV meeting.\nMeeting Topic: Weekly\n"
        "Time: 2026/10/10 15:00-16:00 (GMT+08:00)\nMeeting ID: 936 109 253\nPasscode: 1234"
    )
    assert (r.meeting_code, r.password, r.subject) == ("936109253", "1234", "Weekly")
    assert r.starts_at == datetime(2026, 10, 10, 15, 0, tzinfo=CST)


# ---------------------------------------------------------------- links


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://meeting.tencent.com/p/936109253", True),
        ("https://MEETING.TENCENT.COM/p/936109253", True),
        ("https://meeting.tencent.com:443/p/936109253", True),
        ("wemeet://page/inmeeting?meeting_code=936109253", True),
        ("https://evilvoovmeeting.com/p/936109253", False),
        ("https://evil.com\\@meeting.tencent.com/p/936109253", False),
        ("https://user@meeting.tencent.com/p/936109253", False),
        ("https://meeting.tencent.com.evil.com/p/936109253", False),
    ],
)
def test_meeting_url_allowlist(url, ok):
    assert is_meeting_url(url) is ok


def test_spoofed_link_is_never_the_join_url():
    r = p("https://evilvoovmeeting.com/p/936109253")
    assert (r.join_url, r.meeting_code) == (None, None)


@pytest.mark.parametrize(
    ("text", "url"),
    [
        (
            "链接：https://meeting.tencent.com/dm/AbCdEf123，密码1234，明天下午3点",
            "https://meeting.tencent.com/dm/AbCdEf123",
        ),
        ("https://meeting.tencent.com/p/936109253点击入会", "https://meeting.tencent.com/p/936109253"),
        ("见 https://meeting.tencent.com/p/936109253.", "https://meeting.tencent.com/p/936109253"),
    ],
)
def test_url_stops_at_chinese_and_punctuation(text, url):
    assert p(text).join_url == url


def test_link_code_conflicting_with_code_line():
    r = p("原会议取消 https://meeting.tencent.com/p/1111111111\n新会议 #腾讯会议：936-109-253")
    assert InviteFlag.CODE_CONFLICT in r.flags
    assert r.join_url is None  # never a link to a different meeting


def test_two_links_two_meetings():
    r = p("https://meeting.tencent.com/p/1111111111\nhttps://meeting.tencent.com/p/936109253")
    assert InviteFlag.MULTIPLE_MEETINGS in r.flags


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://meeting.tencent.com/p/936109253", "936109253"),
        ("wemeet://page/inmeeting?meeting_code=936109253", "936109253"),
        ("https://meeting.tencent.com/dm/AbCdEf123", None),
        ("https://wemeet.qq.com/w/5NmV9x", None),
    ],
)
def test_code_from_url(url, code):
    assert code_from_url(url) == code


# ---------------------------------------------------------------- times


def tr(value, **kw):
    return parse_time_range(value, now=NOW, **kw)


def test_cross_midnight_with_two_dates():
    start, end, flags = tr("2026/10/10 23:30-2026/10/11 00:30 (GMT+08:00)")
    assert end - start == timedelta(hours=1) and flags == set()


def test_cross_midnight_without_second_date():
    start, end, flags = tr("2026/10/10 23:30-00:30")
    assert end - start == timedelta(hours=1) and flags == set()


def test_reversed_daytime_range_is_flagged_not_shifted():
    _, _, flags = tr("2026/10/10 16:00-15:00")
    assert InviteFlag.ENDS_BEFORE_START in flags


@pytest.mark.parametrize(
    ("value", "following", "expected_day"),
    [
        ("2026/10/10-2026/11/10 15:00-16:00 (GMT+08:00)", "每周 (周五)", 16),  # 10/10 is a Saturday
        ("2026/09/01-2026/12/31 15:00-16:00", "每周 (周二)", 13),  # forwarded weeks later: next Tuesday
        ("2026/10/01-2026/12/31 09:00-09:30", "每天", 10),  # today 09:30 is over
        ("2026/10/01-2026/12/31 13:00-14:00", "每天", 9),  # today 13:00 is still ahead
        ("2026/10/01-2026/12/31 10:00-11:00", "每个工作日", 12),  # Friday 11:00 is over, next is Monday
        ("2026/10/01-2026/12/31 10:00-11:00", "每月 (15日)", 15),
    ],
)
def test_recurring_resolves_next_occurrence(value, following, expected_day):
    start, end, flags = tr(value, following=following)
    assert InviteFlag.RECURRING in flags
    assert start.day == expected_day and start.month == 10
    assert end - start > timedelta(0)


def test_biweekly_keeps_its_parity():
    start, _, _ = tr("2026/10/06-2026/12/31 15:00-16:00", following="每两周 (周二)")  # first on 10/06
    assert start.date().isoformat() == "2026-10-20"


def test_recurring_with_unknown_rule_asks():
    start, _, flags = tr("2026/10/10-2026/11/10 15:00-16:00", following="自定义规则")
    assert start is None and InviteFlag.TIME_UNCLEAR in flags


def test_finished_series_is_in_the_past():
    start, _, flags = tr("2026/08/01-2026/09/30 15:00-16:00", following="每天")
    assert start is None and InviteFlag.STARTS_IN_PAST in flags


def test_one_off_meeting_in_the_past_is_flagged():
    _, _, flags = tr("2026/10/01 15:00-16:00")
    assert InviteFlag.STARTS_IN_PAST in flags


def test_meeting_in_progress_is_not_past():
    _, _, flags = tr("2026/10/09 11:30-13:00")
    assert InviteFlag.STARTS_IN_PAST not in flags


@pytest.mark.parametrize(
    "value", ["2026/10/10 3:00 PM-4:00 PM", "2026/10/10 3:00下午", "2026/10/10 下午3:00-4:00"]
)
def test_meridiem_words_are_not_guessed(value):
    start, _, flags = tr(value)
    assert start is None and InviteFlag.TIME_UNCLEAR in flags


@pytest.mark.parametrize(
    ("value", "hours"),
    [
        ("2026/10/10 15:00-16:00 (GMT-05:00)", -5),
        ("2026/10/10 15:00-16:00 GMT-5", -5),
        ("2026/10/10 15:00-16:00 UTC+9", 9),
        ("2026/10/10 15:00-16:00 UTC", 0),
    ],
)
def test_zone_marks(value, hours):
    start, _, _ = tr(value)
    assert start.utcoffset() == timedelta(hours=hours)


@pytest.mark.parametrize("value", ["2026/10/10 15:00 GMT+25:00", "2026/10/10 15:00 (GMT+23:60)"])
def test_impossible_offsets_ask_instead_of_crashing(value):
    start, _, flags = tr(value)
    assert start is None and InviteFlag.TIME_UNCLEAR in flags


def test_default_zone_is_shanghai():
    start, _, _ = tr("2026/10/10 15:00-16:00")
    assert start.utcoffset() == timedelta(hours=8)


def test_invalid_date_is_ignored():
    assert tr("2026/13/40 15:00-16:00") == (None, None, set())


# ---------------------------------------------------------------- cards


@pytest.mark.parametrize(
    "pagepath",
    [
        "pages/index/index?code=936109253",
        "pages/sub-preMeeting/join-meeting/join-meeting?scene=m%3D936109253",
    ],
)
def test_miniprogram_card(pagepath):
    r = parse_miniprogram(pagepath, title="产品周会")
    assert r.meeting_code == "936109253"
    assert r.subject == "产品周会"
    assert InviteFlag.MISSING_TIME in r.flags


def test_miniprogram_card_without_code():
    assert InviteFlag.MISSING_CODE in parse_miniprogram("pages/index/index").flags


def test_miniprogram_card_with_overlong_code():
    assert parse_miniprogram("pages/index/index?code=9361092531234").meeting_code is None
