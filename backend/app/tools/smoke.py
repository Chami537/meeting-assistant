"""Connectivity check against the real services configured in .env, before a demo or a deploy.

    python -m app.tools.smoke            (from backend/)

Every check is read-only or cleans up after itself, and none of them is billed: COS writes, reads and deletes
one tiny object under smoke/, ASR only queries a task id that does not exist, the LLM gets a one-line prompt.
Secrets are never printed.
"""

import asyncio
import hashlib
import shutil
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import text

from ..config import Settings, get_settings


@dataclass
class Check:
    name: str
    ok: bool | None  # None: skipped (not configured)
    detail: str


async def check_database(settings: Settings) -> Check:
    if not settings.database_url:
        return Check("数据库", None, "未配置 DATABASE_URL")
    from ..db import get_engine

    async with get_engine().connect() as conn:
        version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
    return Check("数据库", True, f"已连接，迁移版本 {version}")


async def check_cos(settings: Settings, storage=None, http: httpx.AsyncClient | None = None) -> Check:
    if storage is None:
        if not all(
            (settings.cos_bucket, settings.cos_region, settings.cos_secret_id, settings.cos_secret_key)
        ):
            return Check(
                "COS 对象存储", None, "未配置 COS_BUCKET / COS_REGION / COS_SECRET_ID / COS_SECRET_KEY"
            )
        from ..deps import object_storage

        storage = object_storage()
    key = f"smoke/{uuid.uuid4().hex}.txt"
    body = b"meeting-assistant smoke test"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "smoke.txt"
        path.write_bytes(body)
        await storage.upload_file(path, key, content_type="text/plain")
    try:
        stat = await storage.stat(key)
        if stat is None or stat.bytes != len(body) or stat.sha256 != hashlib.sha256(body).hexdigest():
            return Check("COS 对象存储", False, "上传后读回的大小或 sha256 不一致")
        url = await storage.presign_get(key, expires_at=datetime.now(UTC) + timedelta(minutes=5))
        client = http or httpx.AsyncClient(timeout=15)
        try:
            got = await client.get(url)
        finally:
            if http is None:
                await client.aclose()
        if got.status_code != 200 or got.content != body:
            return Check("COS 对象存储", False, f"预签名下载失败：HTTP {got.status_code}")
    finally:
        await storage.delete(key)
    return Check("COS 对象存储", True, "上传、读取元数据、预签名下载、删除均正常")


async def check_asr(settings: Settings, asr=None) -> Check:
    if settings.asr_provider in ("whisper", "funasr", "qwen"):
        return await check_whisper(settings, asr)
    if asr is None:
        if not (settings.asr_secret_id and settings.asr_secret_key):
            return Check("腾讯云 ASR", None, "未配置 ASR_SECRET_ID / ASR_SECRET_KEY")
        from ..integrations.tencentcloud.api import TencentCloudApi
        from ..integrations.tencentcloud.asr import SERVICE, VERSION, TencentAsr

        asr = TencentAsr(
            TencentCloudApi(
                settings.asr_secret_id, settings.asr_secret_key, service=SERVICE, version=VERSION
            ),
            engine=settings.asr_engine_model,
        )
    from ..postprocess.providers import AsrState, ProviderError

    try:
        result = await asr.poll("1")  # a task id that does not exist: free, and proves the signature works
    except ProviderError as exc:
        hint = (
            "密钥错误或没有 ASR 权限"
            if str(exc).startswith("AuthFailure")
            else "请检查账号是否开通录音文件识别"
        )
        return Check("腾讯云 ASR", False, f"{exc}（{hint}）")
    if result.state == AsrState.FAILED and result.error == "FailedOperation.NoSuchTask":
        return Check("腾讯云 ASR", True, f"签名和权限正常，引擎 {settings.asr_engine_model}")
    return Check("腾讯云 ASR", True, f"签名正常（查询返回 {result.state}）")


async def check_whisper(settings: Settings, asr=None) -> Check:
    """Only the health report of the self-hosted job service (faster-whisper or Fun-ASR): no upload."""
    if settings.asr_provider == "qwen":
        name, url, variable = "自建 ASR（Qwen）", settings.asr_qwen_url, "ASR_QWEN_URL"
    elif settings.asr_provider == "funasr":
        name, url, variable = "自建 ASR（Fun-ASR）", settings.asr_funasr_url, "ASR_FUNASR_URL"
    else:
        name, url, variable = "自建 ASR（Whisper）", settings.asr_whisper_url, "ASR_WHISPER_URL"
    if asr is None:
        if not url:
            return Check(name, None, f"未配置 {variable}")
        from ..integrations.whisper import WhisperJobsAsr

        asr = WhisperJobsAsr(url, model=settings.asr_whisper_model)
    from ..postprocess.providers import ProviderError

    try:
        ready = await asr.ready()
    except ProviderError as exc:
        return Check(name, False, f"{exc}（请确认服务已启动，且本机能访问 {url}）")
    detail = f"模型 {ready.get('model')}，{ready.get('device')}，队列空位 {ready.get('queue_available')}"
    expected = {"qwen": "qwen3-asr-0.6b", "funasr": "fun-asr-nano-2512"}.get(settings.asr_provider)
    if expected and ready.get("model") != expected:  # ASR_PROVIDER and the deployed service disagree
        return Check(name, False, f"服务在运行，但{detail}，不是 {expected}（请核对部署的服务和 {variable}）")
    return Check(name, True, f"服务就绪：{detail}")


async def check_llm(settings: Settings, llm=None) -> Check:
    if llm is None:
        from ..deps import llm_client

        try:
            llm = llm_client(settings)  # exactly the production client: a rejected setting shows here
        except RuntimeError as exc:
            return Check("LLM", None, f"未配置：{exc}")
    from ..postprocess.providers import ProviderError

    try:
        reply = await llm.complete_json(
            system='只输出 JSON：{"ok": true}',
            user="连通性测试",
            # A named field: under structured outputs a bare object schema only admits {}.
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
        )
    except ProviderError as exc:
        return Check("LLM", False, str(exc))
    if reply.content.get("ok") is not True:
        return Check("LLM", False, "返回了 JSON，但不是预期内容；请确认模型支持 JSON 模式")
    return Check("LLM", True, f"模型 {settings.llm_model} 支持 JSON 模式输出")


async def check_wecom(settings: Settings, client=None) -> Check:
    if client is None:
        if not (settings.wecom_corp_id and settings.wecom_app_secret):
            return Check("企业微信 / 微信客服", None, "未配置 WECOM_CORP_ID / WECOM_APP_SECRET")
        from ..integrations.wecom.client import WeComClient

        client = WeComClient(settings.wecom_corp_id, settings.wecom_app_secret)
    from ..integrations.wecom.client import WeComApiError

    try:
        accounts = await client.list_accounts()
    except WeComApiError as exc:
        hints = {
            60020: "服务器出口 IP 不在应用的企业可信 IP 中",
            40001: "Secret 错误",
            48002: "应用没有微信客服接口权限",
        }
        return Check(
            "企业微信 / 微信客服", False, f"错误码 {exc.errcode}（{hints.get(exc.errcode, exc.errmsg)}）"
        )
    ids = {a.get("open_kfid") for a in accounts}
    if settings.wecom_kf_open_kfid and settings.wecom_kf_open_kfid not in ids:
        return Check(
            "企业微信 / 微信客服", False, f"WECOM_KF_OPEN_KFID 不在该应用可用的 {len(ids)} 个客服账号中"
        )
    return Check("企业微信 / 微信客服", True, f"access_token 正常，可用客服账号 {len(ids)} 个")


async def check_tools(settings: Settings) -> Check:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        return Check("ffmpeg", False, f"缺少 {', '.join(missing)}（sudo apt install ffmpeg）")
    return Check("ffmpeg", True, "ffmpeg 和 ffprobe 已安装")


async def check_public_url(settings: Settings) -> Check:
    url = settings.public_base_url
    if not url:
        return Check("PUBLIC_BASE_URL", False, "未配置：纪要链接无法生成")
    if not url.startswith("https://"):
        return Check("PUBLIC_BASE_URL", False, "必须是 https 地址（会话 cookie 带 Secure）")
    return Check("PUBLIC_BASE_URL", True, url)


CHECKS: list[Callable[[Settings], Awaitable[Check]]] = [
    check_database,
    check_cos,
    check_asr,
    check_llm,
    check_wecom,
    check_tools,
    check_public_url,
]


async def run(settings: Settings, checks=CHECKS) -> list[Check]:
    results = []
    for check in checks:
        try:
            results.append(await check(settings))
        except Exception as exc:  # a crash in one check must not hide the others; never print the message
            results.append(Check(check.__name__.removeprefix("check_"), False, f"异常 {type(exc).__name__}"))
    return results


def render(results: list[Check]) -> str:
    mark = {True: "✅", False: "❌", None: "⏭ "}
    return "\n".join(f"{mark[r.ok]} {r.name}：{r.detail}" for r in results)


async def main() -> int:
    results = await run(get_settings())
    print(render(results))
    return 1 if any(r.ok is False for r in results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
