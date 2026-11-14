"""The reviewed glossary."""

import csv

import pytest
from app.postprocess import glossary

_read = glossary.terms.__wrapped__  # uncached: each test writes its own file
FIELDS = [
    "术语",
    "分类",
    "释义",
    "全称/别名",
    "常见错写",
    "错写出处(study/)",
    "识别上下文",
    "识别写法",
    "出处",
    "状态",
]


@pytest.fixture
def table(tmp_path, monkeypatch):
    """Writes a glossary; `terms()` reads it from then on."""

    def write(*rows):
        path = tmp_path / "glossary.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for term, asr, spoken, status in rows:
                w.writerow(
                    dict.fromkeys(FIELDS, "")
                    | {"术语": term, "识别上下文": asr, "识别写法": spoken, "状态": status}
                )
        monkeypatch.setattr(glossary, "terms", lambda: _read(path))
        return path

    return write


def test_only_reviewed_rows_count(table):
    table(("募投", "是", "", "通过"), ("两新一大", "是", "", "否决"), ("毛利率", "否", "", "待审"))
    assert [t.term for t in glossary.terms()] == ["募投"]


def test_the_recognizer_gets_the_spoken_spelling(table):
    table(("投资者关系", "是", "IR", "通过"), ("毛利率", "否", "", "通过"))
    assert [(t.term, t.asr) for t in glossary.terms()] == [("投资者关系", "IR"), ("毛利率", None)]


def test_the_shipped_glossary_reads():
    shipped = glossary.terms()
    assert shipped and all(t.term for t in shipped)
    assert ("投资者关系", "IR") in {(t.term, t.asr) for t in shipped}


def test_replacements_name_what_a_misspelling_becomes():
    pairs = dict(glossary.replacements())
    assert pairs["示立证券"] == "示例证券" and pairs["目投项目"] == "募投项目"
    assert pairs["C P O模块"] == "CPO模块" and pairs["C P O"] == "CPO"
    assert not {"资本开始", "入住", "算例", "目投"} & set(pairs)  # real words elsewhere: never replaced blindly
    order = [w for w, _ in glossary.replacements()]
    assert order.index("C P O模块") < order.index("C P O")  # the phrase before the word in it


def test_a_spelling_names_its_term_and_the_transcript_backs_it():
    assert glossary.term_of("IR").term == "投资者关系"  # the spoken spelling
    assert glossary.term_of("募集资金投资项目").term == "募投"  # an alias
    assert glossary.term_of("cpo").term == "共封装光学" and glossary.term_of("硅光芯片") is None
    cpo = glossary.term_of("CPO")
    assert glossary.evidence(cpo, "采用C P O的一个方案")  # a misspelling seen before
    assert not glossary.evidence(cpo, "采用CPU的一个方案")


def test_the_proofreading_list_has_spellings_not_definitions():
    block = glossary.prompt_block()
    assert "募投（又称：募投项目、募集资金投资项目；逐字稿里见过的错写：目投、慕投）" in block
    assert "投资者关系（又称：IR、Investor Relations、投资者关系管理；逐字稿里见过的错写：爱尔）" in block
    assert "未来现金流折现" not in block
