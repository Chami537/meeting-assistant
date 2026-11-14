"""The glossary, glossary.csv: finance, pharma and chip terms as they are spelled, each with a web page that
spells it that way, and the misspellings our recognizers were seen to write.
Only rows marked 通过 (reviewed) are used.

The recognizer's short list (the terms marked for it, in the way they are said: "IR", not 投资者关系) is added
by our transcription service itself (asr_service/glossary.py, which reads this file), after the title any
caller sends: an A/B on one earnings call fixed the terms the list names without the list showing up in the
transcript.

The minutes are checked against it once written (proofread.py): `replacements` are the misspellings marked
可直接替换 (no word of their own, or only in a phrase that is: 示立证券, 目投项目), `prompt_block` is the list
the proofreading request gets, `term_of` and `evidence` back what it proposes.
"""

import csv
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PATH = Path(__file__).with_name("glossary.csv")


@dataclass(frozen=True)
class Term:
    term: str
    definition: str
    aliases: tuple[str, ...]  # full or English names: 募集资金投资项目, IR (for 投资者关系)
    misheard: tuple[str, ...]  # seen in our transcripts: 木头, 牧童 (notes in brackets dropped)
    replace: tuple[str, ...]  # misspellings safe to replace anywhere: "中银建投", "木头项目→募投项目"
    asr: str | None  # how the recognizer gets it, or None when it is not in the recognizer's list

    @property
    def spellings(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.term, *([self.asr] if self.asr else []), *self.aliases)))


def _split(value: str | None) -> tuple[str, ...]:
    """ "a、b / c" as (a, b, c); a note in brackets after a word is dropped: "HR（“…”）" is HR."""
    words = re.split(r"[、/]", re.sub(r"（[^）]*）", "", value or ""))
    return tuple(w for w in (w.strip() for w in words) if w)


@cache
def terms(path: Path = PATH) -> tuple[Term, ...]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["状态"].strip() == "通过"]
    return tuple(
        Term(
            term=r["术语"].strip(),
            definition=r["释义"].strip(),
            aliases=_split(r.get("全称/别名")),
            misheard=_split(r.get("常见错写")),
            replace=tuple(w.strip() for w in (r.get("可直接替换的错写") or "").split("、") if w.strip()),
            asr=(r["识别写法"].strip() or r["术语"].strip()) if r["识别上下文"].strip() == "是" else None,
        )
        for r in rows
        if r["术语"].strip()
    )


def replacements() -> list[tuple[str, str]]:
    """(misspelling, spelling), longest first so a phrase wins over a word in it. An entry is a misspelling
    of the term ("中银建投"), or names what it becomes ("木头项目→募投项目", "F C R伽马→FcγR")."""
    out = []
    for t in terms():
        for entry in t.replace:
            wrong, _, right = entry.partition("→")
            out.append((wrong.strip(), right.strip() or t.term))
    return sorted(out, key=lambda p: len(p[0]), reverse=True)


def prompt_block() -> str:
    """One line a term, no definitions: "募投（又称：募集资金投资项目；逐字稿里见过的错写：木头、牧童）"."""
    lines = []
    for t in terms():
        notes = [f"又称：{'、'.join(t.aliases)}"] if t.aliases else []
        if t.misheard:
            notes.append(f"逐字稿里见过的错写：{'、'.join(t.misheard)}")
        lines.append(f"{t.term}（{'；'.join(notes)}）" if notes else t.term)
    return "\n".join(lines)


def term_of(spelling: str) -> Term | None:
    """The term `spelling` is one of the spellings of (exactly, case and spaces aside)."""
    key = _key(spelling)
    return next((t for t in terms() if any(_key(s) == key for s in t.spellings)), None)


def evidence(term: Term, transcript: str) -> bool:
    """The transcript says the term, under one of its spellings or a misspelling seen before."""
    text = _key(transcript)
    return any(len(k := _key(s)) >= 2 and k in text for s in (*term.spellings, *term.misheard))


def _key(text: str) -> str:
    return text.replace(" ", "").lower()
