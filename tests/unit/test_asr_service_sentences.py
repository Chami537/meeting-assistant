"""Sentence times from the forced aligner's unit times (asr_service/sentences.py), with the aligner's tokenizer
copied here: the service passes in the real one (qwen_asr is only in its image)."""

import unicodedata

from asr_service.sentences import Sentence, plan, sentences


def _cjk(ch: str) -> bool:
    code = ord(ch)
    return any(
        lo <= code <= hi
        for lo, hi in (
            (0x4E00, 0x9FFF),
            (0x3400, 0x4DBF),
            (0x20000, 0x2A6DF),
            (0x2A700, 0x2B73F),
            (0x2B740, 0x2B81F),
            (0x2B820, 0x2CEAF),
            (0xF900, 0xFAFF),
        )
    )


def tokenize(text: str) -> list[str]:
    """qwen_asr 0.0.4's Qwen3ForceAlignProcessor.tokenize_space_lang: split at spaces, keep letters, digits
    and apostrophes; a CJK character is a unit, a run of the others another."""
    units = []
    for part in text.split():
        run = ""
        for ch in part:
            if not (ch == "'" or unicodedata.category(ch)[0] in "LN"):
                continue
            if _cjk(ch):
                if run:
                    units.append(run)
                    run = ""
                units.append(ch)
            else:
                run += ch
        if run:
            units.append(run)
    return units


def at(n: int) -> list[tuple[float, float]]:
    """Unit k from k to k + 0.5 seconds."""
    return [(float(k), k + 0.5) for k in range(n)]


def test_clauses_are_joined_with_spaces_so_the_aligner_sees_their_units_one_after_another():
    planned = plan("我们收入四千七百八十六万元，同比增长。U G A的信号呢？好", tokenize)
    assert planned.text == "我们收入四千七百八十六万元， 同比增长。 U G A的信号呢？ 好"
    assert [n for _, n in planned.clauses] == [13, 4, 7, 1]
    assert tokenize(planned.text) == [u for clause, _ in planned.clauses for u in tokenize(clause)]


def test_a_sentence_runs_from_its_first_unit_to_its_last_and_the_last_one_ends_with_the_chunk():
    planned = plan("收入四千万元，同比增长。明年呢？好", tokenize)  # 6 + 4 + 3 + 1 units
    assert sentences(planned, at(14)) == [
        Sentence(0.0, 9.5, "收入四千万元，同比增长。"),
        Sentence(10.0, 12.5, "明年呢？"),
        Sentence(13.0, 13.5, "好"),  # no full stop: the chunk ended
    ]


def test_letters_and_digits_together_are_one_unit():
    planned = plan("XB1002和XB1003。", tokenize)
    assert planned.units == 3
    assert sentences(planned, at(3)) == [Sentence(0.0, 2.5, "XB1002和XB1003。")]


def test_a_clause_without_units_goes_with_the_sentence_before_it():
    planned = plan("好的。……，然后呢", tokenize)
    assert [n for _, n in planned.clauses] == [2, 0, 3]
    assert sentences(planned, at(5)) == [Sentence(0.0, 1.5, "好的。……，"), Sentence(2.0, 4.5, "然后呢")]
    leading = plan("，好的", tokenize)  # before any unit: kept for the first sentence
    assert sentences(leading, at(2)) == [Sentence(0.0, 1.5, "，好的")]


def test_no_sentences_when_the_unit_count_differs_or_there_is_nothing_to_align():
    planned = plan("同比增长。", tokenize)
    assert sentences(planned, at(3)) == [] and sentences(planned, at(5)) == []
    silent = plan("。。", tokenize)
    assert silent.units == 0 and sentences(silent, []) == []
