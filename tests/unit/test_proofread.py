"""Proofreading the minutes against the glossary: what is replaced, what a proposed fix needs to be applied."""

import asyncio

import pytest
from app.postprocess import minutes as mm
from app.postprocess import proofread
from app.postprocess.providers import LlmReply, ProviderError

SEGMENTS = [
    {"i": 0, "start_ms": 0, "end_ms": 1, "speaker": None, "text": "请示立证券的分析师提问"},
    {"i": 1, "start_ms": 1, "end_ms": 2, "speaker": None, "text": "新产品采用C P O的一个方案"},
    {"i": 2, "start_ms": 2, "end_ms": 3, "speaker": None, "text": "幕投资金用于新建产线，硅基光电项目"},
]


def written():
    return mm.Minutes(
        summary="示立证券分析师提问（逐字稿中另有写法：示立证券）。",
        key_points=[
            {
                "text": "新产品采用CPU",
                "details": ["幕投资金用于产线", "共封装光学方案"],
                "evidence_segments": [1, 2],
            }
        ],
        key_facts=[{"text": "硅基光电项目已投产", "evidence_segments": [2]}],
    )


class FakeLlm:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def complete_json(self, *, system, user, schema):
        assert schema is proofread.SCHEMA and system == proofread.SYSTEM_PROMPT
        self.prompts.append(user)
        if isinstance(self.reply, Exception):
            raise self.reply
        return LlmReply(self.reply, input_tokens=10, output_tokens=5)


async def test_registered_misspellings_then_the_backed_proposals():
    llm = FakeLlm(
        {
            "fixes": [
                {
                    "wrong": "CPU",
                    "right": "CPO",
                },  # the transcript says C P O, a known misspelling of it
                {"wrong": "幕投", "right": "募投"},  # new to the glossary, but said and sounds the same
                {"wrong": "硅基光电", "right": "硅光芯片"},  # not a glossary spelling
                {"wrong": "分析师", "right": "国谈"},  # nothing in the transcript backs 国谈
                {"wrong": "不在纪要里", "right": "募投"},
                {"wrong": "CPO", "right": "CPO"},  # nothing to change
                {
                    "wrong": "共封装",
                    "right": "共封装光学",
                },  # part of the spelling: 共封装光学 would become 共封装光学光学
            ]
        }
    )
    minutes = written()
    loop = asyncio.get_running_loop()
    tokens = await proofread.proofread(llm, minutes, SEGMENTS, deadline=loop.time() + 60, label="t")
    assert tokens == (10, 5)
    # registered: replaced before the request; the note quoting the transcript keeps its words
    assert minutes.summary == "示例证券分析师提问（逐字稿中另有写法：示立证券）。"
    assert minutes.key_points[0].text == "新产品采用CPO"
    assert minutes.key_points[0].details == ["募投资金用于产线", "共封装光学方案"]
    assert minutes.key_facts[0].text == "硅基光电项目已投产"
    assert [(f.heard, f.written) for f in minutes.name_fixes] == [
        ("示立证券", "示例证券"),
        ("CPU", "CPO"),
        ("幕投", "募投"),
    ]
    [prompt] = llm.prompts  # the minutes as fixed so far, the glossary, the cited lines
    assert "示例证券分析师提问" in prompt and "募投（又称：" in prompt
    assert "[1] 新产品采用C P O的一个方案" in prompt and "[0]" not in prompt  # [0] is not cited


@pytest.mark.parametrize("reply", [ProviderError("HTTP 502"), {"fixes": "none"}, {}])
async def test_a_failed_or_odd_reply_leaves_the_registered_fixes(reply):
    minutes = written()
    loop = asyncio.get_running_loop()
    await proofread.proofread(FakeLlm(reply), minutes, SEGMENTS, deadline=loop.time() + 60, label="t")
    assert [(f.heard, f.written) for f in minutes.name_fixes] == [("示立证券", "示例证券")]
    assert minutes.key_points[0].text == "新产品采用CPU"


@pytest.mark.parametrize(
    ("a", "b", "alike"),
    [
        ("幕投", "募投", True),
        ("进现值", "净现值", True),
        ("硅基光电", "硅光芯片模组", False),
        ("集采", "国谈", False),
    ],
)
def test_sounds_alike(a, b, alike):
    assert proofread.sounds_alike(a, b) is alike
