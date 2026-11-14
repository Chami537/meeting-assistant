"""Offline stand-in for the model (LLM_API=fake): no network, no key, the same reply for the same request.

It exists so the whole service can run on a laptop without a model account (the offline mode in README.md)
and so the request paths can be exercised end to end: the minutes, the article summary (with its points for
long articles) and the proofreading request all go through the real prompts, schemas and validation; only
the answer is made here. The answer is extractive: every item copies one line of the transcript, or one
sentence of an article chunk, and cites where it came from, so the citation and figure checks pass the way
they would for a careful model. It is not a summarizer and must not be configured in production: every
summary it writes starts with OFFLINE_MARK and `model` says what it is.
"""

import re
from typing import Any

from ..postprocess.providers import LlmReply, ProviderError

OFFLINE_MARK = "【离线替身】"
MODEL = "offline-fake"

_LINE = re.compile(r"^\[(\d+)\]\s*(?:\d{1,2}:\d{2}:\d{2}\s+)?" r"(?:[^:：\s]{1,20}[:：]\s*)?(.*)$")
_CHUNK = re.compile(r"^\[(\d+)\]\s?(.*)$")
_POINT = re.compile(r"^\[第\d+部分\]\s*(.*?)（原文编号：([\d、]+)）\s*$")
_SENTENCE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
_DIGIT = re.compile(r"\d")
_OUTLOOK = (
    ("预计", "预计"), ("预期", "预计"), ("目标", "目标"), ("力争", "目标"), ("计划", "计划"), ("将", "计划")
)
_RISK = ("风险", "不确定", "压力", "困难")


class FakeLlm:
    name = "offline"
    model = MODEL
    region = "local"

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        properties = schema.get("properties", {})
        if "key_points" in properties:
            content = _minutes(user)
        elif "sentences" in properties:
            content = _summary(user)
        elif "points" in properties:
            content = _points(user)
        elif "fixes" in properties:
            content = {"fixes": []}  # proofreading: nothing to fix, the glossary step stays a no-op
        else:
            raise ProviderError("offline stand-in: unknown request schema", permanent=True)
        return LlmReply(content, input_tokens=len(system) + len(user), output_tokens=len(str(content)))


def _between(text: str, tag: str) -> str:
    start, end = text.find(f"<{tag}"), text.find(f"</{tag}>")
    if start < 0 or end < 0:
        return ""
    return text[text.find(">", start) + 1 : end]


def _transcript(user: str) -> list[tuple[int, str]]:
    lines = []
    for raw in _between(user, "transcript").splitlines():
        if (m := _LINE.match(raw.strip())) and m.group(2).strip():
            lines.append((int(m.group(1)), m.group(2).strip()))
    return lines


def _minutes(user: str) -> dict[str, Any]:
    lines = _transcript(user)
    if not lines:
        raise ProviderError("offline stand-in: no transcript in the request", permanent=True)
    cut = len(lines) // 4 or 1
    points = [lines[k] for k in range(0, len(lines), cut)][:4]
    facts = [(i, t) for i, t in lines if _DIGIT.search(t)][:8]
    outlook = []
    for i, t in lines:
        kind = next((k for word, k in _OUTLOOK if word in t), None)
        if kind and len(outlook) < 6:
            outlook.append({"text": t[:500], "kind": kind, "timeframe": None, "evidence_segments": [i]})
    risks = [{"text": t[:500], "evidence_segments": [i]} for i, t in lines if any(w in t for w in _RISK)][:3]
    head = "；".join(t for _, t in lines[:3])
    return {
        "name_fixes": [],
        "meeting_type": "其他",
        "meeting_date": None,
        "summary": f"{OFFLINE_MARK}{head}"[:400],
        "key_points": [{"text": t[:300], "details": [], "evidence_segments": [i]} for i, t in points],
        "key_facts": [{"text": t[:300], "evidence_segments": [i]} for i, t in facts],
        "outlook": outlook,
        "decisions": [],
        "action_items": [],
        "risks": risks,
    }


def _chunks(user: str) -> list[tuple[int, str]]:
    chunks, current = [], None
    for raw in _between(user, "article").splitlines():
        if m := _CHUNK.match(raw):
            current = [int(m.group(1)), m.group(2)]
            chunks.append(current)
        elif current is not None:  # a chunk's text runs over several lines
            current[1] += "\n" + raw
    return [(i, t) for i, t in chunks if t.strip()]


def _first_sentences(chunks: list[tuple[int, str]], limit: int) -> list[dict[str, Any]]:
    out, length = [], 0
    for i, text in chunks:
        sentence = next((s.strip() for s in _SENTENCE.findall(text.replace("\n", " ")) if s.strip()), "")
        if not sentence:
            continue
        if not sentence.endswith(("。", "！", "？", "!", "?", "；", ";")):
            sentence += "。"
        if out and length + len(sentence) > limit:
            break
        out.append({"text": sentence, "evidence_chunks": [i]})
        length += len(sentence)
    return out


def _summary(user: str) -> dict[str, Any]:
    if "<points>" in user:
        sentences = []
        for raw in _between(user, "points").splitlines():
            if m := _POINT.match(raw.strip()):
                text = m.group(1).strip()
                sentences.append({"text": text if text.endswith("。") else text + "。",
                                  "evidence_chunks": [int(n) for n in m.group(2).split("、")]})
        sentences = sentences[:4]
    else:
        sentences = _first_sentences(_chunks(user), 300)
    if not sentences:
        raise ProviderError("offline stand-in: nothing to summarize", permanent=True)
    sentences[0] = {**sentences[0], "text": OFFLINE_MARK + sentences[0]["text"]}
    return {"sentences": sentences}


def _points(user: str) -> dict[str, Any]:
    return {"points": _first_sentences(_chunks(user), 600)[:8]}
