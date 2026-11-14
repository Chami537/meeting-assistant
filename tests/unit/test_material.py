"""Fallback-path material: file kinds from the first bytes, document text, transcript layouts."""

import io
import zipfile

import pytest
from app.intake import material as m
from app.intake.material import Kind


@pytest.mark.parametrize(
    ("head", "kind"),
    [
        (b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00", Kind.MEDIA),
        (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00", Kind.MEDIA),
        (b"ID3\x04\x00\x00\x00\x00\x00\x00", Kind.MEDIA),
        (b"\xff\xfb\x90\x64\x00", Kind.MEDIA),
        (b"\xff\xf1\x50\x80\x00", Kind.MEDIA),  # aac adts
        (b"RIFF\x24\x08\x00\x00WAVEfmt ", Kind.MEDIA),
        (b"#!AMR\n\x3c", Kind.MEDIA),
        (b"OggS\x00\x02", Kind.MEDIA),
        (b"\x1a\x45\xdf\xa3\x9f\x42", Kind.MEDIA),
        (b"%PDF-1.7\n", Kind.PDF),
        (b"PK\x03\x04\x14\x00", Kind.DOCUMENT),
        ("发言人1 00:00:01\n大家好".encode(), Kind.DOCUMENT),
        ("会议纪要".encode("gb18030"), Kind.DOCUMENT),
        (b"\x89PNG\r\n\x1a\n\x00\x00", Kind.UNKNOWN),
        (bytes(range(256)), Kind.UNKNOWN),
    ],
)
def test_classify(head, kind):
    assert m.classify(head) == kind


def test_head_cut_inside_a_character_is_still_text():
    data = "会议纪要：确定上线时间".encode()
    assert m.classify(data[:-1]) == Kind.DOCUMENT  # the last character is cut in half


def docx(paragraphs, *, extra=None):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document {ns}><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", extra if extra is not None else xml)
    return buf.getvalue()


def test_docx_text():
    assert m.extract_text(docx(["说话人01 00:00:05", "我们开始吧"])) == "说话人01 00:00:05\n我们开始吧"


def test_docx_zip_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(m, "MAX_DOCX_XML_BYTES", 1000)
    assert m.extract_text(docx(["x" * 5000])) is None


def test_docx_with_entities_is_refused():
    evil = '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]><d>&a;</d>'
    assert m.extract_text(docx([], extra=evil)) is None


@pytest.mark.parametrize("data", [b"PK\x03\x04garbage", b"\x00\x01binary", b"PK\x03\x04" + b"\x00" * 30])
def test_unreadable_documents(data):
    assert m.extract_text(data) is None


def test_text_is_bounded(monkeypatch):
    monkeypatch.setattr(m, "MAX_EXTRACT_CHARS", 10)
    assert m.extract_text(("会" * 50).encode()) == "会" * 10


def test_docx_element_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(m, "MAX_XML_ELEMENTS", 1000)
    assert m.extract_text(docx(["<w:r/>" * 2000])) is None


def test_docx_with_corrupt_deflate_stream():
    data = bytearray(docx(["正常的文字"] * 200))
    start = data.index(b"word/document.xml") + len(b"word/document.xml")
    for i in range(start + 10, start + 60):  # scramble the compressed member body
        data[i] ^= 0x5A
    assert m.extract_text(bytes(data)) is None


def test_utf16_text_and_legacy_word():
    text = "说话人01 00:00:05\n大家好"
    for encoding in ("utf-16", "utf-16-be"):
        raw = text.encode(encoding)
        if encoding == "utf-16-be":
            raw = b"\xfe\xff" + raw
        assert m.classify(raw[:4096]) == Kind.DOCUMENT
        assert m.extract_text(raw) == text
    assert m.classify(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(100)) == Kind.LEGACY_DOC


def test_timed_transcript_with_headers():
    text = (
        "说话人01 00:00:05\n大家好，今天讨论上线。\n第二句。\n\n说话人02 00:01:10\n好的。\n张三 1:02:03\n收尾"
    )
    assert m.parse_transcript(text) == [
        {
            "i": 0,
            "start_ms": 5000,
            "end_ms": 70_000,
            "speaker": "说话人01",
            "text": "大家好，今天讨论上线。 第二句。",
        },
        {"i": 1, "start_ms": 70_000, "end_ms": 3_723_000, "speaker": "说话人02", "text": "好的。"},
        {"i": 2, "start_ms": 3_723_000, "end_ms": 3_723_000, "speaker": "张三", "text": "收尾"},
    ]


@pytest.mark.parametrize(
    "line",
    [
        "[00:00:05] 张三：大家好",
        "00:00:05 张三: 大家好",
        "张三(00:00:05): 大家好",
        "张三（00:05）大家好",
        "张三 00:00:05：大家好",
    ],
)
def test_inline_layouts(line):
    [first, _] = m.parse_transcript(f"{line}\n[00:00:09] 李四：好")
    assert (first["speaker"], first["text"]) == ("张三", "大家好")
    assert first["start_ms"] == 5000


def test_copied_minutes_become_plain_lines():
    text = "## 会议摘要\n确定下周三上线。\n\n- 待办：张三写测试\n会议时间：10:00"
    segments = m.parse_transcript(text)
    assert [s["text"] for s in segments] == [
        "## 会议摘要",
        "确定下周三上线。",
        "- 待办：张三写测试",
        "会议时间：10:00",
    ]
    assert all(s["start_ms"] is None and s["speaker"] is None for s in segments)


def test_segment_count_is_bounded(monkeypatch):
    monkeypatch.setattr(m, "MAX_SEGMENTS", 3)
    assert len(m.parse_transcript("\n".join(f"第{i}行" for i in range(10)))) == 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://meeting.tencent.com/crm/AbCdEf1234", True),
        ("看这个 https://meeting.tencent.com/v2/cloud-record/share?id=1&from=3", True),
        ("https://meeting.tencent.com/cw/xyz", True),
        ("https://pre.txmeeting.tencent.com/user-center/shared-record-info?id=1", True),
        ("https://meeting.tencent.com/dm/AbCd 入会链接", False),
        ("https://evil.example/meeting.tencent.com/crm/x", False),
    ],
)
def test_share_links(text, expected):
    assert m.is_recording_share_link(text) is expected


def test_docx_deep_nesting_is_refused():
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    xml = f"<w:document {ns}>" + "<a>" * 5000 + "</a>" * 5000 + "</w:document>"
    assert m.extract_text(docx([], extra=xml)) is None
