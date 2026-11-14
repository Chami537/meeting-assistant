"""The AI meeting page's result (postprocess/meeting_result.py): the shape its Go backend decodes
(its ParseResult schema 1.0) and what the page shows of it."""

from datetime import UTC, datetime

from app.postprocess import minutes as mm
from app.postprocess.meeting_result import paragraphs, parse_result
from app.postprocess.providers import AsrSegment, AsrSentence

OPENING = AsrSegment(
    0,
    8000,
    "欢迎参加晨川科技业绩说明会。今年收入3529万元。",
    "发言人1",
    sentences=(
        AsrSentence(0, 3000, "欢迎参加晨川科技业绩说明会。"),
        AsrSentence(3200, 7900, "今年收入3529万元。"),
    ),
)
NOD = AsrSegment(8000, 9000, "好。", "发言人2", sentences=(AsrSentence(8100, 8600, "好。"),))
UNTIMED = AsrSegment(9000, 20000, "新产线预计6月底调试。", "发言人1")  # its chunk's alignment failed
SILENT = AsrSegment(20000, 21000, "   ")
SEGMENTS = [UNTIMED, OPENING, NOD, SILENT]  # out of order, as nothing promises otherwise

MINUTES = {  # as_columns of the minutes written from SEGMENTS: i 0 = OPENING, 1 = NOD, 2 = UNTIMED
    "summary": "晨川科技业绩说明会，收入增长，新产线6月底调试。",
    "meeting_type": "公司交流",
    "key_points": [
        {"text": "收入增长", "details": ["今年收入3529万元。", "同比增长"], "evidence_segments": [0]}
    ],
    "key_facts": [
        {"text": "收入3529万元", "evidence_segments": [0]},
        {"text": "新产线6月底调试。", "evidence_segments": [2]},
    ],
    "outlook": [{"text": "新产线调试。", "kind": "计划", "timeframe": "6月底", "evidence_segments": [2]}],
    "decisions": [{"text": "下周发布公告", "evidence_segments": [1]}],
    "action_items": [
        {"task": "整理材料", "owner": "未知", "due": None, "evidence_segments": [1, 99]},
        {"task": "发布公告", "owner": "王总", "due": "下周", "evidence_segments": [2]},
    ],
    "risks": [],
    "name_fixes": [],
}
NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


def result(minutes=MINUTES, **kw):
    timed = [
        {"start_ms": s.start_ms, "end_ms": s.end_ms, "speaker": s.speaker, "text": s.text} for s in SEGMENTS
    ]
    numbered, sources = mm.number_segments(timed)
    assert [seg["text"] for seg in numbered] == [OPENING.text, NOD.text, UNTIMED.text]  # as MINUTES assumes
    kw.setdefault("title", "03-晨川科技20260318")
    return parse_result(SEGMENTS, sources, minutes, task_id="a" * 32, now=NOW, **kw)


def test_one_paragraph_per_sentence_in_time_order_with_a_nod_joined_to_the_one_before():
    paras, owners = paragraphs(SEGMENTS)
    assert paras == [
        {"id": 1, "text": "欢迎参加晨川科技业绩说明会。", "start_time_ms": 0, "end_time_ms": 3000},
        {"id": 2, "text": "今年收入3529万元。好。", "start_time_ms": 3200, "end_time_ms": 8600},
        {"id": 3, "text": "新产线预计6月底调试。", "start_time_ms": 9000, "end_time_ms": 20000},
    ]
    assert owners == [[3], [1, 2], [2], []]  # per segment as given: UNTIMED, OPENING, NOD, SILENT


def test_the_result_has_the_shape_the_go_backend_decodes():
    body = result()
    assert body["schema_version"] == "1.0" and body["generated_at"] == "2026-11-10T08:00:00+00:00"
    assert body["source"] == {"type": "media_url", "url": "", "task_id": "a" * 32}  # no signed link
    assert body["transcription"]["audio_info"] == {"duration": 0}  # the page keeps the browser's measure
    assert (
        body["transcription"]["text"]
        == "欢迎参加晨川科技业绩说明会。今年收入3529万元。好。新产线预计6月底调试。"
    )
    paras = body["transcription"]["paragraphs"]
    assert [p["id"] for p in paras] == [1, 2, 3]
    numbers = [p[k] for p in paras for k in ("id", "start_time_ms", "end_time_ms")]
    numbers += [
        i
        for section in ("key_points", "decisions", "action_items")
        for item in body["summary"][section]
        for i in [item["id"], *item["paragraph_ids"]]
    ]
    assert all(type(n) is int for n in numbers)  # Go's int64: a 3200.0 would never decode


def test_the_minutes_fill_the_four_sections_the_page_shows():
    summary = result()["summary"]
    assert summary["title"] == "03-晨川科技20260318"
    assert summary["overview"] == MINUTES["summary"]
    assert summary["key_points"] == [
        {"id": 1, "title": "收入增长", "text": "今年收入3529万元；同比增长。", "paragraph_ids": [1, 2]},
        {
            "id": 2,
            "title": "重要数据与事实",
            "text": "收入3529万元；新产线6月底调试。",
            "paragraph_ids": [1, 2, 3],
        },
        {"id": 3, "title": "计划与预期", "text": "计划：新产线调试（6月底）。", "paragraph_ids": [3]},
    ]  # no risks, so no third folded point
    assert summary["decisions"] == [{"id": 1, "text": "下周发布公告", "paragraph_ids": [2]}]
    assert summary["action_items"] == [  # the page shows `text`; an unknown owner and no due are left out
        {"id": 1, "text": "整理材料", "paragraph_ids": [2]},
        {"id": 2, "text": "发布公告", "owner": "王总", "due": "下周", "paragraph_ids": [3]},
    ]


def test_without_minutes_the_transcript_stays_and_the_overview_says_why():
    body = result(None, failure="录音太长，纪要写不下")
    assert len(body["transcription"]["paragraphs"]) == 3
    assert body["summary"] == {
        "title": "03-晨川科技20260318",
        "overview": "纪要没有生成：录音太长，纪要写不下",
        "key_points": [],  # an array, never null: a null blanks the page
        "decisions": [],
        "action_items": [],
    }


def test_a_missing_title_is_empty_and_a_long_one_fits_the_column():
    assert result(title=None)["summary"]["title"] == ""  # the Go backend then keeps the meeting's title
    assert len(result(title="长" * 300)["summary"]["title"]) == 255
