"""Times for the sentences of a chunk, from the forced aligner's time for every unit of its text: a CJK
character, or a run of letters and digits (punctuation is not aligned). Free of torch and qwen_asr, so it is
tested locally; the engine passes in the aligner's own tokenizer.

A chunk's raw text (numerals not yet converted: the recognizer wrote what was said) is cut into clauses, each
with the punctuation after it, and the clauses are joined with spaces for the aligner. The aligner's tokenizer
splits at spaces first, so its units are exactly the clauses' units one after another, and every clause knows
its first and last unit. Sentences end at 。！？； (and ASCII !?;); the chunk's last sentence ends with the
chunk. On one recorded call: 188 chunks, 466 sentences, a median of 4.3 s; cut out at their times and
recognized again, 87% start and 88% end with the same two characters (times spread by character count: 65%
and 47%).
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

_CLAUSE = re.compile(r"[^，,。！？!?；;：:]+[，,。！？!?；;：:]*|[，,。！？!?；;：:]+")
_SENTENCE_END = tuple("。！？!?；;")


@dataclass(frozen=True)
class Sentence:
    start: float  # seconds from the start of the chunk
    end: float
    text: str  # raw, as the recognizer wrote it


@dataclass(frozen=True)
class Plan:
    text: str  # what the aligner is given
    clauses: tuple[tuple[str, int], ...]  # each clause and its number of units

    @property
    def units(self) -> int:
        return sum(n for _, n in self.clauses)


def plan(raw: str, tokenize: Callable[[str], Sequence[str]]) -> Plan:
    clauses = tuple((clause, len(tokenize(clause))) for clause in _CLAUSE.findall(raw))
    return Plan(" ".join(clause for clause, _ in clauses), clauses)


def sentences(planned: Plan, times: Sequence[tuple[float, float]]) -> list[Sentence]:
    """The sentences of `planned` from the aligner's (start, end) of every unit, in seconds; none when the
    aligner returned another number of units than the plan has (the chunk keeps its own times only). A clause
    without units ("……，") belongs to the sentence before it."""
    if not times or len(times) != planned.units:
        return []
    out: list[Sentence] = []
    text, first, last, k = "", None, 0, 0  # the open sentence: its text, its first and last unit
    for clause, n in planned.clauses:
        if n:
            first = k if first is None else first
            last, k = k + n - 1, k + n
            text += clause
        elif first is not None or not out:  # in the open sentence, or before the first unit
            text += clause
        else:
            out[-1] = replace(out[-1], text=out[-1].text + clause)
        if first is not None and clause.endswith(_SENTENCE_END):
            out.append(Sentence(times[first][0], times[last][1], text))
            text, first = "", None
    if first is not None:
        out.append(Sentence(times[first][0], times[last][1], text))
    return out
