"""Material a user hands in on the fallback path (design doc §4 路径 D): what kind of file it is, the text
of an exported transcript or minutes (txt, Word), and that text as numbered transcript segments.

WeChat KF file messages carry only a media_id: no name, size or type (official doc 94670). The kind is told
from the first bytes; names and Content-Type are hints at most. Everything here is untrusted input: archive
members are size-checked before reading (zip bombs), XML goes through defusedxml, output is bounded.
"""

import io
import re
import zipfile
from dataclasses import dataclass
from enum import StrEnum

from defusedxml import ElementTree

HEAD_BYTES = 4096  # a multiple of 16, as WeChat media Range requests require
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # WeChat KF delivers files up to 20 MB
MAX_DOCX_XML_BYTES = 8 * 1024 * 1024  # 90k characters of text with Word markup is a fraction of this
MAX_XML_ELEMENTS = 200_000  # bounds parse time and memory independently of the byte size
MAX_XML_DEPTH = 64  # real Word bodies nest a dozen levels
MAX_EXTRACT_CHARS = 400_000  # read this much to tell "too long" from "fits"; callers enforce their limit
MAX_TEXT_CHARS = 90_000  # what fits one minutes request (MAX_TRANSCRIPT_CHARS with per-line labels)
MAX_SEGMENTS = 5000

_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy Office (.doc)


class Kind(StrEnum):
    MEDIA = "MEDIA"  # audio or video container: transcribe
    DOCUMENT = "DOCUMENT"  # plain text or Word: read
    PDF = "PDF"  # recognised but not supported: ask for txt or Word
    LEGACY_DOC = "LEGACY_DOC"  # Word 97-2003: ask for .docx or txt
    UNKNOWN = "UNKNOWN"


def classify(head: bytes) -> Kind:
    if head.startswith(_UTF16_BOMS):  # before the frame-sync test: FF FE also looks like an mp3 header
        return Kind.DOCUMENT
    if head.startswith(_OLE):
        return Kind.LEGACY_DOC
    if len(head) >= 12 and head[4:8] == b"ftyp":  # mp4, m4a, mov, 3gp
        return Kind.MEDIA
    if head.startswith((b"ID3", b"#!AMR", b"OggS", b"fLaC", b"FLV", b"\x1a\x45\xdf\xa3")):  # mp3 ... mkv
        return Kind.MEDIA
    if head.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"):  # asf: wma, wmv
        return Kind.MEDIA
    if head.startswith(b"RIFF") and head[8:12] in (b"WAVE", b"AVI "):
        return Kind.MEDIA
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:  # mp3 / aac frame sync
        return Kind.MEDIA
    if head.startswith(b"%PDF"):
        return Kind.PDF
    if head.startswith(b"PK\x03\x04"):  # zip: a Word document if it has word/document.xml
        return Kind.DOCUMENT
    if decode_text(head, partial=True) is not None:
        return Kind.DOCUMENT
    return Kind.UNKNOWN


def decode_text(data: bytes, *, partial: bool = False) -> str | None:
    """UTF-16 with a BOM, UTF-8 (with or without BOM) or GB18030 text; None for binary. `partial`: data may
    end mid-character."""
    if data.startswith(_UTF16_BOMS):
        if partial:
            data = data[: len(data) - len(data) % 2]
        candidates = [("utf-16", data)]
    else:
        if b"\x00" in data:
            return None
        candidates = [("utf-8-sig", data), ("gb18030", data)]
    for encoding, raw in candidates:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            if partial and exc.start >= len(raw) - 3:  # a multi-byte character cut by the head limit
                text = raw[: exc.start].decode(encoding, errors="ignore")
            else:
                continue
        if "\x00" in text:
            return None
        printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
        if text and printable / len(text) > 0.95:
            return text
    return None


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(data: bytes) -> str | None:
    """Paragraph text of word/document.xml, parsed incrementally: bytes, elements and collected characters
    are all capped, so a small crafted file cannot cost much memory or time. Any failure means "not a
    readable Word document" (the input is untrusted: zlib, zip and XML errors alike)."""
    paragraphs: list[str] = []
    collected = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                return None
            with archive.open(info) as member:
                elements = depth = 0
                for event, node in ElementTree.iterparse(member, events=("start", "end")):
                    if event == "start":  # counted when built: deep nesting builds everything first
                        elements += 1
                        depth += 1
                        if elements > MAX_XML_ELEMENTS or depth > MAX_XML_DEPTH:
                            return None
                        continue
                    depth -= 1
                    if node.tag != f"{_W}p":
                        continue
                    parts = []
                    for child in node.iter():
                        if child.tag == f"{_W}t" and child.text:
                            parts.append(child.text)
                        elif child.tag == f"{_W}tab":
                            parts.append("\t")
                        elif child.tag in (f"{_W}br", f"{_W}cr"):
                            parts.append("\n")
                    node.clear()
                    paragraph = "".join(parts)
                    paragraphs.append(paragraph)
                    collected += len(paragraph) + 1
                    if collected > MAX_EXTRACT_CHARS:
                        break
    except Exception:  # BadZipFile, KeyError, zlib.error, EOFError, NotImplementedError, XML/entity errors
        return None
    return "\n".join(paragraphs)


def extract_text(data: bytes) -> str | None:
    """Text of a document file (txt or docx), at most MAX_EXTRACT_CHARS; None if it is neither."""
    text = docx_text(data) if data.startswith(b"PK\x03\x04") else decode_text(data)
    if text is None:
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:MAX_EXTRACT_CHARS] or None


# ---------------------------------------------------------------- transcript text -> segments

_TIME = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"
_SPEAKER = r"[^\s:：\[\]()（）]{1,20}"
_LINE_PATTERNS = [
    # 张三 00:01:23            (speaker and time on their own line, text below)
    re.compile(rf"^(?P<speaker>{_SPEAKER}(?: {_SPEAKER})?)\s+(?P<time>{_TIME})\s*$"),
    # [00:01:23] 张三：内容 / 00:01:23 张三: 内容
    re.compile(rf"^\[?(?P<time>{_TIME})\]?\s+(?P<speaker>{_SPEAKER})\s*[:：]\s*(?P<text>.+)$"),
    # 张三(00:00:05): 内容 / 张三（00:05）内容
    re.compile(rf"^(?P<speaker>{_SPEAKER})\s*[(（](?P<time>{_TIME})[)）]\s*[:：]?\s*(?P<text>.*)$"),
    # 张三 00:00:05：内容
    re.compile(rf"^(?P<speaker>{_SPEAKER})\s+(?P<time>{_TIME})\s*[:：]\s*(?P<text>.+)$"),
    # [00:01:23] 内容
    re.compile(rf"^\[(?P<time>{_TIME})\]\s*(?P<text>.+)$"),
]


def _ms(value: str) -> int:
    value = value.replace(",", ".")
    seconds = 0.0
    for part in value.split(":"):
        seconds = seconds * 60 + float(part)
    return round(seconds * 1000)


@dataclass
class _Segment:
    start_ms: int | None
    speaker: str | None
    lines: list[str]


def parse_transcript(text: str) -> list[dict]:
    """Segments {i, start_ms, end_ms, speaker, text}. Timed layouts ("发言人 00:01:23" headers,
    "[00:01] 张三：…") keep speaker and time; anything else (copied minutes, Markdown) becomes one segment per
    non-empty line without time. Times are relative to the recording start, end_ms is the next segment's
    start."""
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
    segments: list[_Segment] = []
    timed = 0
    for line in lines:
        if not line:
            continue
        match = next((m for p in _LINE_PATTERNS if (m := p.match(line))), None)
        if match:
            timed += 1
            groups = match.groupdict()
            body = (groups.get("text") or "").strip()
            segments.append(_Segment(_ms(groups["time"]), groups.get("speaker"), [body] if body else []))
        elif segments and segments[-1].start_ms is not None:
            segments[-1].lines.append(line)  # text under a "speaker time" header
        else:
            segments.append(_Segment(None, None, [line]))
        if len(segments) > MAX_SEGMENTS:
            break
    if timed < 2:  # not a timed transcript: plain lines, no invented times or speakers
        segments = [_Segment(None, None, [ln]) for ln in lines if ln][:MAX_SEGMENTS]
    out = []
    for seg in segments:
        body = " ".join(part for part in seg.lines if part).strip()
        if body:
            out.append({"start_ms": seg.start_ms, "speaker": seg.speaker, "text": body})
    for n, seg in enumerate(out):
        seg["i"] = n
        following = next((s["start_ms"] for s in out[n + 1 :] if s["start_ms"] is not None), None)
        seg["end_ms"] = (
            following if seg["start_ms"] is not None and following is not None else seg["start_ms"]
        )
    return [{k: seg[k] for k in ("i", "start_ms", "end_ms", "speaker", "text")} for seg in out]


def display_name(raw: str | None) -> str | None:
    """A file name as the user sent it, for display only: no control characters or path separators."""
    if not raw:
        return None
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", raw).strip()[:200]
    return name or None


# ---------------------------------------------------------------- shared links we cannot open

_SHARE_LINK = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*(?:meeting\.tencent\.com|txmeeting\.tencent\.com)/"
    r"(?:crm/|cw/|v2/cloud-record/|user-center/shared-record-info)",
    re.I,
)


def is_recording_share_link(text: str) -> bool:
    """A 腾讯会议 cloud-recording / minutes share link: its page needs the sharer's permission, so the server
    cannot read it; the user has to export the file instead."""
    return bool(_SHARE_LINK.search(text))
