"""What the meetings page shows, from our transcript and minutes: the result its Go backend polls for
(ParseResult schema 1.0, as its original implementation wrote it).

Paragraphs are the recognizer's timed sentences, one each, so that a click plays from that sentence; one
of fewer than 5 characters ("好。", "哦。") goes on the end of the paragraph before it. A segment without
sentence times is one paragraph. Ids count from 1 in time order: the page orders paragraphs by id and finds
the one playing by start time.

The page shows a title, an overview, key points (title, text, and "查看出处" from their paragraph_ids),
decisions and to-dos (text, owner, due); its Go backend keeps no other section. So the minutes' key facts,
outlook and risks go in as three more key points at the end. The page shows no line breaks: lists are joined
with "；". Every list is an array (a null key_points blanks the page), every number an integer.
"""

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from .minutes import clean_text
from .providers import AsrSegment

MIN_PARAGRAPH_CHARS = 5
MAX_TITLE_CHARS = 255  # the page's title column
FOLDED = (("key_facts", "重要数据与事实"), ("outlook", "计划与预期"), ("risks", "风险与待定事项"))
_ENDS = "。；;，,、 "


def paragraphs(segments: Sequence[AsrSegment]) -> tuple[list[dict[str, Any]], list[list[int]]]:
    """The page's paragraphs {id, text, start_time_ms, end_time_ms}, and for every segment, in the order
    given, the ids of the paragraphs holding its text."""
    pieces: list[tuple[int, int, str, int]] = []  # start, end, text, the segment's position
    for k, seg in enumerate(segments):
        own = [(s.start_ms, s.end_ms, text, k) for s in seg.sentences if (text := clean_text(s.text).strip())]
        if not own and (text := clean_text(seg.text).strip()):
            own = [(seg.start_ms, seg.end_ms, text, k)]
        pieces += own
    pieces.sort(key=lambda p: (p[0], p[1]))
    out: list[dict[str, Any]] = []
    owners: list[set[int]] = [set() for _ in segments]
    for start, end, text, k in pieces:
        if out and _chars(text) < MIN_PARAGRAPH_CHARS:
            last = out[-1]
            last["text"] += text
            last["end_time_ms"] = max(last["end_time_ms"], end)
        else:
            out.append(
                {"id": len(out) + 1, "text": text, "start_time_ms": start, "end_time_ms": max(end, start)}
            )
        owners[k].add(out[-1]["id"])
    return out, [sorted(ids) for ids in owners]


def parse_result(
    segments: Sequence[AsrSegment],
    sources: Sequence[int],
    minutes: dict[str, Any] | None,
    *,
    title: str | None,
    task_id: str,
    failure: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The page's result. `sources`: for every segment the minutes were written from (number i), its
    position in `segments` (minutes.number_segments). `minutes`: as_columns of the minutes, or None, with
    `failure` saying why, when none were written: the transcript and the player still work."""
    paras, owners = paragraphs(segments)

    def cited(evidence: Iterable[int]) -> list[int]:
        ids: set[int] = set()
        for i in evidence:
            if isinstance(i, int) and 0 <= i < len(sources):
                ids.update(owners[sources[i]])
        return sorted(ids)

    return {
        "schema_version": "1.0",
        "generated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "source": {"type": "media_url", "url": "", "task_id": task_id[:64]},  # not the signed audio link
        # 0: the page keeps the duration the browser measured at upload
        "transcription": {
            "audio_info": {"duration": 0},
            "text": "".join(p["text"] for p in paras),
            "paragraphs": paras,
        },
        "summary": _summary(
            minutes, title=(title or "").strip()[:MAX_TITLE_CHARS], cited=cited, failure=failure
        ),
    }


def _summary(
    minutes: dict[str, Any] | None,
    *,
    title: str,
    cited: Callable[[Iterable[int]], list[int]],
    failure: str | None,
) -> dict[str, Any]:
    if minutes is None:
        overview = f"纪要没有生成：{failure}" if failure else ""
        return {"title": title, "overview": overview, "key_points": [], "decisions": [], "action_items": []}
    points = [
        {
            "id": n,
            "title": p["text"],
            "text": _join(p.get("details") or []),
            "paragraph_ids": cited(p["evidence_segments"]),
        }
        for n, p in enumerate(minutes.get("key_points") or [], 1)
    ]
    for key, name in FOLDED:
        if items := minutes.get(key) or []:
            evidence = [i for item in items for i in item["evidence_segments"]]
            text = _join(_line(key, item) for item in items)
            points.append(
                {"id": len(points) + 1, "title": name, "text": text, "paragraph_ids": cited(evidence)}
            )
    decisions = [
        {"id": n, "text": d["text"], "paragraph_ids": cited(d["evidence_segments"])}
        for n, d in enumerate(minutes.get("decisions") or [], 1)
    ]
    actions = []
    for n, a in enumerate(minutes.get("action_items") or [], 1):
        item: dict[str, Any] = {"id": n, "text": a["task"], "paragraph_ids": cited(a["evidence_segments"])}
        if (owner := (a.get("owner") or "").strip()) and owner != "未知":
            item["owner"] = owner
        if due := (a.get("due") or "").strip():
            item["due"] = due
        actions.append(item)
    return {
        "title": title,
        "overview": minutes.get("summary") or "",
        "key_points": points,
        "decisions": decisions,
        "action_items": actions,
    }


def _line(key: str, item: dict[str, Any]) -> str:
    if key != "outlook":
        return item["text"]
    when = f"（{item['timeframe']}）" if item.get("timeframe") else ""
    return f"{item['kind']}：{item['text'].strip().rstrip(_ENDS)}{when}"


def _join(items: Iterable[str]) -> str:
    parts = [p for t in items if (p := t.strip().rstrip(_ENDS))]
    return "；".join(parts) + "。" if parts else ""


def _chars(text: str) -> int:
    return sum(ch.isalnum() for ch in text)
