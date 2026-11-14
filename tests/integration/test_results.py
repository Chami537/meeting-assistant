"""Result page (design doc §5.5): GET never consumes, POST redeems once, session cookie, logs, deletion."""

import json
import os
import re
from datetime import timedelta

import httpx
import pytest
from app.deps import clock, db_engine, keyring
from app.main import app
from app.security.keyring import Keyring
from app.services import results
from factories import NOW, make_task
from meeting_protocol import TaskKind, TaskStatus
from sqlalchemy import text

RING = Keyring({1: os.urandom(32)}, 1)
SEGMENTS = [
    {"i": 0, "start_ms": 0, "end_ms": 3000, "speaker": "1", "text": "看 https://evil.example/x <b>粗体</b>"},
    {"i": 1, "start_ms": 65_000, "end_ms": 70_000, "speaker": None, "text": "张三负责测试"},
]


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
async def env(engine, clean):
    tick = Clock()
    app.dependency_overrides.update({db_engine: lambda: engine, clock: tick, keyring: lambda: RING})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        yield client, tick
    app.dependency_overrides.clear()


async def completed_task(engine, *, status=TaskStatus.COMPLETED, kind=TaskKind.LIVE_MEETING):
    async with engine.begin() as conn:
        task_id = await make_task(conn, status, kind=kind)
        user_id = await conn.scalar(text("SELECT user_id FROM meeting_tasks WHERE id = :t"), {"t": task_id})
        transcript = await conn.scalar(
            text(
                "INSERT INTO transcripts (task_id, source, body) VALUES (:t, 'worker_asr', :b) RETURNING id"
            ),
            {"t": task_id, "b": json.dumps({"segments": SEGMENTS})},
        )
        await conn.execute(
            text(
                """
                INSERT INTO meeting_minutes (task_id, transcript_id, summary, decisions, action_items,
                  model_provider, model_name)
                VALUES (:t, :tr, '<script>alert(1)</script>摘要', :d, :a, 'vendor', 'm1')
                """
            ),
            {
                "t": task_id,
                "tr": transcript,
                "d": json.dumps([{"text": "下周上线", "evidence_segments": [0, 1]}]),
                "a": json.dumps([{"task": "写测试", "owner": "张三", "due": None, "evidence_segments": [1]}]),
            },
        )
        link = await results.mint_link(
            conn, task_id=task_id, user_id=user_id, now=NOW, base_url="https://x.example/"
        )
    return task_id, link.removeprefix("https://x.example")


async def rows(engine, sql, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).all()


async def test_link_flow(engine, env):
    client, _ = env
    task_id, path = await completed_task(engine)
    assert re.fullmatch(r"/r/[A-Za-z0-9_-]{43}", path)

    landing = await client.get(path)
    assert landing.status_code == 200
    assert "查看纪要" in landing.text and "摘要" not in landing.text
    assert landing.headers["cache-control"] == "no-store"
    assert landing.headers["referrer-policy"] == "no-referrer"
    assert (await rows(engine, "SELECT used_at FROM result_access_tokens"))[0].used_at is None  # GET is free

    redeemed = await client.post(path)
    assert redeemed.status_code == 303
    assert redeemed.headers["location"] == f"/r/t/{task_id}"
    cookie = redeemed.headers["set-cookie"]
    for attr in ("HttpOnly", "Secure", "SameSite=strict", "Path=/r/", "Max-Age=604800"):
        assert attr.lower() in cookie.lower()

    page = await client.get(f"/r/t/{task_id}")
    assert page.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;摘要" in page.text
    assert "<script>" not in page.text
    assert "&lt;b&gt;粗体&lt;/b&gt;" in page.text
    assert '<a href="https://evil' not in page.text  # transcript links stay plain text
    assert "写测试（负责人：张三）" in page.text
    assert "依据 #0、#1" in page.text
    assert "00:01:05" in page.text
    assert "模型：vendor/m1" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]

    again = await client.post(path)  # same device: the cookie still opens the page
    assert (again.status_code, again.headers["location"]) == (303, f"/r/t/{task_id}")

    stranger = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")
    async with stranger:
        assert (await stranger.post(path)).status_code == 410
        assert (await stranger.get(f"/r/t/{task_id}")).status_code == 403
    actions = [r.action for r in await rows(engine, "SELECT action FROM result_access_logs ORDER BY id")]
    assert actions == ["VIEW", "DENIED"]
    ip_hash = (await rows(engine, "SELECT ip_hash FROM result_access_logs LIMIT 1"))[0].ip_hash
    assert ip_hash and len(ip_hash) == 64


async def test_minutes_v2_sections(engine, env):
    """Key points with details, facts, plans and expectations, name fixes: in reading order, all escaped."""
    client, _ = env
    task_id, path = await completed_task(engine)
    sections = {
        "key_points": [{"text": "<i>观点</i>", "details": ["理由 <u>一</u>"], "evidence_segments": [0]}],
        "key_facts": [{"text": "营收 3.2 亿元", "evidence_segments": [1]}],
        "outlook": [
            {"text": "提交申请", "kind": "预计", "timeframe": "<b>明年</b>", "evidence_segments": [1]},
            {"text": "扩产", "kind": "计划", "timeframe": None, "evidence_segments": [0]},
        ],
        "name_fixes": [
            {"heard": "陈川<x>", "written": "晨川", "checked": True},
            {"heard": "一季度", "written": "<Q1>", "checked": False},
        ],
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE meeting_minutes SET key_points = :key_points, key_facts = :key_facts, "
                "outlook = :outlook, name_fixes = :name_fixes WHERE task_id = :t"
            ),
            {"t": task_id, **{k: json.dumps(v) for k, v in sections.items()}},
        )
    await client.post(path)
    page = (await client.get(f"/r/t/{task_id}")).text
    order = [
        page.index(h) for h in ("摘要", "主要观点", "重要数据与事实", "计划与预期", "决定", "待办", "逐字稿")
    ]
    assert order == sorted(order)
    assert "<li>&lt;i&gt;观点&lt;/i&gt;" in page and "<ul><li>理由 &lt;u&gt;一&lt;/u&gt;</li></ul>" in page
    assert '<span class="tag">预计</span>提交申请（&lt;b&gt;明年&lt;/b&gt;）' in page
    assert '<span class="tag">计划</span>扩产 <span class="muted">（依据 #0）</span>' in page
    assert "按会议标题和术语表更正了逐字稿里的写法：陈川&lt;x&gt; → 晨川</p>" in page
    assert (
        '<p class="note">以下写法是 AI 改的，会议标题里没有依据，请对照逐字稿核实：一季度 → &lt;Q1&gt;</p>'
        in page
    )
    assert "<i>" not in page and "<b>明年" not in page


async def test_minutes_written_before_v2_show_without_the_new_sections(engine, env):
    client, _ = env
    task_id, path = await completed_task(engine)  # key_points etc. take their defaults
    await client.post(path)
    page = (await client.get(f"/r/t/{task_id}")).text
    assert "下周上线" in page
    assert not any(h in page for h in ("主要观点", "重要数据与事实", "计划与预期", "更正了逐字稿"))
    assert "<h1>会议纪要</h1>" in page and "会议类型" not in page  # no name or type before v3


@pytest.mark.parametrize(
    ("source", "shown"),
    [
        ("文件名", "会议时间：2026-03-18 · 会议类型：公司交流"),
        ("上传日期", "会议时间：2026-03-18（上传日期） · 会议类型：公司交流"),
    ],
)
async def test_minutes_v3_heading_names_the_meeting_its_date_and_type(engine, env, source, shown):
    client, _ = env
    task_id, path = await completed_task(engine, kind=TaskKind.USER_UPLOAD)  # no scheduled time
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO task_inputs (task_id, kind, file_name, consumed_at) "
                "VALUES (:t, 'STAGED_OBJECT', '03-晨川<科技>20260318.mp3', :n)"
            ),
            {"t": task_id, "n": NOW},
        )
        await conn.execute(
            text(
                "UPDATE meeting_minutes SET meeting_type = '公司交流', meeting_date = '2026-03-18', "
                "meeting_date_source = :s WHERE task_id = :t"
            ),
            {"t": task_id, "s": source},
        )
    await client.post(path)
    page = (await client.get(f"/r/t/{task_id}")).text
    assert "<h1>会议纪要：03-晨川 科技 20260318</h1>" in page  # the cleaned file name, as the prompt has it
    assert f'<p class="muted">{shown}</p>' in page
    assert "<title>会议纪要 2026-03-18 公司交流</title>" in page


async def test_expired_and_revoked_links(engine, env):
    client, tick = env
    _, path = await completed_task(engine)
    tick.now = NOW + results.LINK_TTL
    assert (await client.post(path)).status_code == 410
    tick.now = NOW
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE result_access_tokens SET revoked_at = :n"), {"n": NOW})
    assert (await client.post(path)).status_code == 410


async def test_unknown_or_malformed_token(env):
    client, _ = env
    assert (await client.post("/r/" + "A" * 43)).status_code == 410
    assert (await client.get("/r/short")).status_code == 422


async def test_session_expires_and_is_bound_to_its_task(engine, env):
    client, tick = env
    task_a, path_a = await completed_task(engine)
    task_b, _ = await completed_task(engine)
    await client.post(path_a)
    cookie = client.cookies.get(f"ma_s_{task_a.hex}")
    client.cookies.set(f"ma_s_{task_b.hex}", cookie, domain="test", path="/r/")  # replay on another task
    assert (await client.get(f"/r/t/{task_b}")).status_code == 403
    tick.now = NOW + results.SESSION_TTL
    assert (await client.get(f"/r/t/{task_a}")).status_code == 403


async def test_only_completed_tasks_show(engine, env):
    client, _ = env
    task_id, path = await completed_task(engine, status=TaskStatus.SUMMARIZING)
    await client.post(path)
    assert (await client.get(f"/r/t/{task_id}")).status_code == 403


async def test_delete(engine, env):
    client, _ = env
    task_id, path = await completed_task(engine)
    await client.post(path)
    assert (await client.get(f"/r/t/{task_id}/delete")).status_code == 200
    refused = await client.post(f"/r/t/{task_id}/delete", headers={"origin": "https://evil.example"})
    assert refused.status_code == 403
    done = await client.post(f"/r/t/{task_id}/delete", headers={"origin": "https://test"})
    assert done.status_code == 200
    assert "ma_s_" in done.headers["set-cookie"] and "max-age=0" in done.headers["set-cookie"].lower()
    [token] = await rows(engine, "SELECT revoked_at FROM result_access_tokens")
    assert token.revoked_at == NOW
    [task] = await rows(engine, "SELECT retention_until FROM meeting_tasks WHERE id = :t", t=task_id)
    assert task.retention_until == NOW
    assert (await client.get(f"/r/t/{task_id}")).status_code == 403
    actions = [r.action for r in await rows(engine, "SELECT action FROM result_access_logs ORDER BY id")]
    assert actions == ["DELETE_REQUEST"]


async def test_view_hidden_once_retention_passed(engine, env):
    client, _ = env
    task_id, path = await completed_task(engine)
    await client.post(path)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE meeting_tasks SET retention_until = :n"), {"n": NOW - timedelta(seconds=1)}
        )
    assert (await client.get(f"/r/t/{task_id}")).status_code == 403
