"""Result page (design doc §5.5). Server-rendered HTML without scripts (api/pages.py); every value is
escaped and links in the transcript are not clickable (prompt-injection defence, §7.5).

    GET  /r/{token}               landing page with a "查看纪要" button; does not consume the token
    POST /r/{token}               redeems the token, sets the session cookie, redirects to the page
    GET  /r/t/{task_id}           the minutes, for a valid session cookie
    GET  /r/t/{task_id}/delete    confirmation page
    POST /r/t/{task_id}/delete    revokes links and sessions, schedules the purge
"""

import html
from datetime import datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from ..deps import clock, db_engine, keyring
from ..postprocess.minutes import segment_label
from ..security.keyring import Keyring
from ..services import results
from ..services.results import TOKEN_PATTERN, ResultView
from .pages import SECURITY_HEADERS, client_info, page, same_origin

router = APIRouter(prefix="/r", include_in_schema=False)

Token = Annotated[str, Path(pattern=TOKEN_PATTERN)]
COOKIE_PATH = "/r/"
_OUTCOME_NOTE = {
    "INTERRUPTED": "录音过程中曾经中断，纪要可能不完整。",
    "LIMIT_REACHED": "录音达到了时长上限，之后的内容没有记录。",
    "STOPPED_BY_USER": "录音按你的要求提前停止。",
}
EXPIRED_TEXT = "链接已失效。请在微信里回复“查看纪要”获取新的链接。"


def _cookie(task_id: UUID) -> str:
    return f"ma_s_{task_id.hex}"


@router.get("/{token}")
async def landing(token: Token) -> HTMLResponse:
    # The same page for every token: WeChat's scanner and previews learn nothing and consume nothing.
    return page(
        "会议纪要",
        "<h1>会议纪要</h1><p>点击下方按钮查看本次会议纪要。"
        "链接只能使用一次，打开后 7 天内可在本设备上再次查看。</p>"
        '<form method="post"><button type="submit">查看纪要</button></form>',
    )


@router.post("/{token}")
async def redeem(
    token: Token,
    request: Request,
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
    ring: Keyring = Depends(keyring),
) -> Response:
    ip, agent = client_info(request)
    async with engine.begin() as conn:
        session = await results.redeem(conn, token, now=now)
        if session is None:
            known = await results.find_token(conn, token)
            if known is not None:
                token_id, task_id = known
                # Already redeemed on this device: the session cookie still opens the page.
                cookie = request.cookies.get(_cookie(task_id))
                if await results.check_session(conn, task_id, cookie, now=now) is not None:
                    return RedirectResponse(f"/r/t/{task_id}", status_code=303, headers=SECURITY_HEADERS)
                await results.log_access(
                    conn,
                    task_id=task_id,
                    token_id=token_id,
                    action="DENIED",
                    ip=ip,
                    user_agent=agent,
                    keyring=ring,
                )
    if session is None:
        return page("链接已失效", f"<p>{EXPIRED_TEXT}</p>", status=410)
    resp = RedirectResponse(f"/r/t/{session.task_id}", status_code=303, headers=SECURITY_HEADERS)
    resp.set_cookie(
        _cookie(session.task_id),
        session.secret,
        max_age=int(results.SESSION_TTL.total_seconds()),
        path=COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return resp


async def _authorized(
    conn, request: Request, task_id: UUID, action: str, *, now: datetime, ring: Keyring
) -> UUID | None:
    token_id = await results.check_session(conn, task_id, request.cookies.get(_cookie(task_id)), now=now)
    ip, agent = client_info(request)
    if token_id is not None:
        await results.log_access(
            conn, task_id=task_id, token_id=token_id, action=action, ip=ip, user_agent=agent, keyring=ring
        )
    return token_id


@router.get("/t/{task_id}")
async def view(
    task_id: UUID,
    request: Request,
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
    ring: Keyring = Depends(keyring),
) -> HTMLResponse:
    async with engine.begin() as conn:
        token_id = await _authorized(conn, request, task_id, "VIEW", now=now, ring=ring)
        result = await results.load_view(conn, task_id, now=now) if token_id else None
    if result is None:
        return page("无法查看", f"<p>{EXPIRED_TEXT}</p>", status=403)
    return _render(result, now=now)


@router.get("/t/{task_id}/delete")
async def confirm_delete(
    task_id: UUID,
    request: Request,
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
) -> HTMLResponse:
    async with engine.connect() as conn:
        token_id = await results.check_session(conn, task_id, request.cookies.get(_cookie(task_id)), now=now)
    if token_id is None:
        return page("无法操作", f"<p>{EXPIRED_TEXT}</p>", status=403)
    return page(
        "删除会议数据",
        "<h1>删除本次会议数据</h1><p>将删除这场会议的录音、逐字稿和纪要，所有查看链接立即失效，删除后无法恢复。</p>"
        '<form method="post"><button class="danger" type="submit">确认删除</button></form>'
        f'<p><a href="/r/t/{task_id}">返回</a></p>',
    )


@router.post("/t/{task_id}/delete")
async def delete(
    task_id: UUID,
    request: Request,
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
    ring: Keyring = Depends(keyring),
) -> HTMLResponse:
    if not same_origin(request):
        return page("无法操作", "<p>请求来源不正确。</p>", status=403)
    async with engine.begin() as conn:
        token_id = await _authorized(conn, request, task_id, "DELETE_REQUEST", now=now, ring=ring)
        if token_id is not None:
            await results.request_deletion(conn, task_id, now=now)
    if token_id is None:
        return page("无法操作", f"<p>{EXPIRED_TEXT}</p>", status=403)
    resp = page(
        "已删除",
        "<h1>已提交删除</h1><p>这场会议的录音、逐字稿和纪要会在几分钟内删除，查看链接已全部失效。</p>",
    )
    resp.delete_cookie(_cookie(task_id), path=COOKIE_PATH, secure=True, httponly=True, samesite="strict")
    return resp


def _render(r: ResultView, *, now: datetime) -> HTMLResponse:
    esc = html.escape
    tz = ZoneInfo(r.timezone)
    heading = f"会议纪要：{esc(r.meeting_title)}" if r.meeting_title else "会议纪要"
    parts = [f"<h1>{heading}</h1>"]
    # The date and type after the heading, for whoever files the minutes (minutes-v3; none before).
    when, tab = None, ["会议纪要"]
    if r.scheduled_at:
        when = f"{r.scheduled_at.astimezone(tz):%Y-%m-%d %H:%M}"
        tab.append(f"{r.scheduled_at.astimezone(tz):%Y-%m-%d}")
    elif r.meeting_date:
        when = f"{r.meeting_date:%Y-%m-%d}" + ("（上传日期）" if r.meeting_date_source == "上传日期" else "")
        tab.append(f"{r.meeting_date:%Y-%m-%d}")
    meta = [f"会议时间：{when}"] if when else []
    if r.meeting_type:
        meta.append(f"会议类型：{esc(r.meeting_type)}")
        tab.append(r.meeting_type)
    if meta:
        parts.append(f'<p class="muted">{" · ".join(meta)}</p>')
    if note := _OUTCOME_NOTE.get(r.recording_outcome or ""):
        parts.append(f'<p class="note">{note}</p>')
    parts.append(f"<h2>摘要</h2><p>{esc(r.summary)}</p>")

    def cite(item: dict) -> str:
        refs = "、".join(f"#{i}" for i in item.get("evidence_segments", []))
        return f' <span class="muted">（依据 {refs}）</span>' if refs else ""

    if r.key_points:
        rows = []
        for k in r.key_points:
            details = "".join(f"<li>{esc(d)}</li>" for d in k.get("details", []))
            rows.append(f"<li>{esc(k['text'])}{cite(k)}{f'<ul>{details}</ul>' if details else ''}</li>")
        parts.append(f"<h2>主要观点</h2><ul>{''.join(rows)}</ul>")
    if r.key_facts:
        items = "".join(f"<li>{esc(f['text'])}{cite(f)}</li>" for f in r.key_facts)
        parts.append(f"<h2>重要数据与事实</h2><ul>{items}</ul>")
    if r.outlook:
        rows = []
        for o in r.outlook:
            when = f"（{esc(o['timeframe'])}）" if o.get("timeframe") else ""
            rows.append(f'<li><span class="tag">{esc(o["kind"])}</span>{esc(o["text"])}{when}{cite(o)}</li>')
        parts.append(f"<h2>计划与预期</h2><ul>{''.join(rows)}</ul>")
    if r.decisions:
        items = "".join(f"<li>{esc(d['text'])}{cite(d)}</li>" for d in r.decisions)
        parts.append(f"<h2>决定</h2><ul>{items}</ul>")
    if r.action_items:
        rows = []
        for a in r.action_items:
            due = f"，截止 {esc(a['due'])}" if a.get("due") else ""
            rows.append(f"<li>{esc(a['task'])}（负责人：{esc(a.get('owner') or '未知')}{due}）{cite(a)}</li>")
        parts.append(f"<h2>待办</h2><ul>{''.join(rows)}</ul>")
    if r.risks:
        items = "".join(f"<li>{esc(k['text'])}{cite(k)}</li>" for k in r.risks)
        parts.append(f"<h2>风险与待定事项</h2><ul>{items}</ul>")
    checked = [f for f in r.name_fixes if f.get("checked", True)]
    unchecked = [f for f in r.name_fixes if not f.get("checked", True)]
    if checked:
        fixes = "、".join(f"{esc(f['heard'])} → {esc(f['written'])}" for f in checked)
        parts.append(f'<p class="muted">纪要中按会议标题和术语表更正了逐字稿里的写法：{fixes}</p>')
    if unchecked:
        fixes = "、".join(f"{esc(f['heard'])} → {esc(f['written'])}" for f in unchecked)
        parts.append(f'<p class="note">以下写法是 AI 改的，会议标题里没有依据，请对照逐字稿核实：{fixes}</p>')
    segs = "".join(
        f'<div class="seg"><span class="muted">#{s["i"]} {esc(segment_label(s).rstrip(": "))}</span> '
        f"{esc(s['text'])}</div>"
        for s in r.segments
    )
    parts.append(f"<details><summary>逐字稿（{len(r.segments)} 段）</summary>{segs}</details>")
    model = f"，模型：{esc(r.model)}" if r.model else ""
    parts.append(
        f'<p class="muted">纪要由 AI 根据录音自动生成，可能有错漏，请以逐字稿为准{model}。</p>'
        f'<p><a href="/r/t/{r.task_id}/delete">删除本次会议数据</a></p>'
    )
    tag = f"…{r.user_tag} {now.astimezone(tz):%Y-%m-%d %H:%M}"
    return page(" ".join(tab), "".join(parts), watermark=tag)
