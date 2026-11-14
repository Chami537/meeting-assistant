"""The offline stand-in model (LLM_API=fake): the requests the real pipelines send it, the shape it answers
with, and that the answers pass the production validation (citations, figures)."""

import asyncio

import pytest
from app.integrations.fake_llm import OFFLINE_MARK, FakeLlm
from app.postprocess import article_summary as asum
from app.postprocess import minutes as mm
from app.postprocess.providers import ProviderError
from app.postprocess.summary import write_minutes

SEGMENTS = [
    {"i": 0, "start_ms": 0, "end_ms": 6000, "speaker": "发言人1", "text": "欢迎参加示例科技业绩交流会。"},
    {"i": 1, "start_ms": 6000, "end_ms": 12000, "speaker": "发言人2", "text": "三季度营业收入1.2亿元，同比增长15%。"},
    {"i": 2, "start_ms": 12000, "end_ms": 18000, "speaker": "发言人1", "text": "我们计划在明年一季度正式量产。"},
    {"i": 3, "start_ms": 18000, "end_ms": 24000, "speaker": "发言人2", "text": "原材料成本压力还在，这是主要的风险。"},
    {"i": 4, "start_ms": 24000, "end_ms": 30000, "speaker": "发言人1", "text": "毛利率是42%。"},
]


async def test_the_minutes_path_writes_valid_minutes():
    llm = FakeLlm()
    loop = asyncio.get_running_loop()
    written = await write_minutes(llm, SEGMENTS, title="03-晨川科技20260318", deadline=loop.time() + 30,
                                  label="t")
    m = written.minutes
    assert OFFLINE_MARK in m.summary and m.summary.startswith(OFFLINE_MARK)
    assert m.key_points and all(k.evidence_segments for k in m.key_points)
    assert m.key_facts and m.outlook and m.risks
    assert m.outlook[0].kind in ("计划", "预计", "目标")
    # every citation names a segment that exists: the production validator ran on it
    for item in m.cited():
        assert all(0 <= i < len(SEGMENTS) for i in item.evidence_segments)


async def test_the_minutes_prompt_is_the_production_one():
    llm = FakeLlm()
    loop = asyncio.get_running_loop()
    written = await write_minutes(llm, SEGMENTS, title="03-晨川科技20260318", deadline=loop.time() + 30,
                                  label="t")
    assert written.minutes.key_points  # the stand-in parsed the production <transcript> layout
    assert llm.model == "offline-fake" and llm.region == "local"


async def test_no_transcript_is_a_permanent_error():
    with pytest.raises(ProviderError) as exc:
        await FakeLlm().complete_json(system="s", user="<transcript>\n</transcript>",
                                      schema=mm.MINUTES_JSON_SCHEMA)
    assert exc.value.permanent


async def test_the_summary_path_copies_chunks_and_cites_them():
    llm = FakeLlm()
    loop = asyncio.get_running_loop()
    written = await asum.write_summary(
        llm,
        ["示例乳业2025年收入120.4亿元。", "维持买入评级，目标价16.5元。", "公司计划新建两座工厂。"],
        title="示例证券_2026-10-13_示例乳业",
        deadline=loop.time() + 30,
        label="t",
    )
    assert written.mode == "whole" and written.summary.startswith(OFFLINE_MARK)
    assert "120.4" in written.summary and "16.5" in written.summary


async def test_the_grouped_summary_path_reads_the_points():
    llm = FakeLlm()
    loop = asyncio.get_running_loop()
    chunks = [f"第{i}部分收入{100 + i}.5亿元。" + "正文" * 15_000 for i in range(4)]  # over MAX_WHOLE_CHARS: grouped
    written = await asum.write_summary(llm, chunks, title=None, deadline=loop.time() + 60, label="t")
    assert written.mode == "grouped" and written.groups > 1
    assert written.summary.startswith(OFFLINE_MARK)


async def test_the_proofreading_request_is_answered_with_no_fixes():
    from app.postprocess.proofread import SCHEMA, SYSTEM_PROMPT
    reply = await FakeLlm().complete_json(system=SYSTEM_PROMPT, user="<glossary>…</glossary>", schema=SCHEMA)
    assert reply.content == {"fixes": []}


async def test_an_unknown_schema_is_a_permanent_error():
    with pytest.raises(ProviderError) as exc:
        await FakeLlm().complete_json(system="s", user="u", schema={"type": "object", "properties": {}})
    assert exc.value.permanent
