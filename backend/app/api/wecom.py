"""WeChat KF callback (design doc §3): URL verification within 1 s, POST answered within 5 s.

The POST handler only verifies, decrypts, stores the sync token and schedules a pull; the reconciliation job
covers any pull lost to a crash, since WeCom does not guarantee delivery of every callback.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from ..config import Settings, get_settings
from ..deps import clock, db_engine, keyring, wecom_client, wecom_crypto
from ..integrations.wecom.client import WeComClient
from ..integrations.wecom.crypto import CallbackCrypto, WeComCryptoError
from ..security.keyring import Keyring
from ..services.kf_sync import record_callback_token, sync_account

log = logging.getLogger(__name__)
router = APIRouter()

MAX_CALLBACK_BYTES = 64 * 1024  # a KF callback envelope is well under 2 KB


async def _pull(engine: AsyncEngine, client: WeComClient, **kwargs) -> None:
    """The response is already sent: a failed pull is logged (type only, as in the runner) and left to the
    reconciliation pull."""
    try:
        await sync_account(engine, client, **kwargs)
    except Exception as exc:
        log.error("callback pull failed: %s", type(exc).__name__)


async def _bounded_body(request: Request) -> bytes | None:
    """The endpoint is public and the signature can only be checked after parsing: refuse large bodies
    before reading them into memory (chunked bodies are counted as they stream in)."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_CALLBACK_BYTES):
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_CALLBACK_BYTES:
            return None
    return bytes(body)


@router.get("/webhooks/wecom/kf", response_class=PlainTextResponse)
async def verify_url(
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
    crypto: CallbackCrypto = Depends(wecom_crypto),
):
    try:
        return PlainTextResponse(crypto.verify_url(msg_signature, timestamp, nonce, echostr))
    except WeComCryptoError as exc:
        log.warning("wecom url verification rejected: %s", exc)
        return PlainTextResponse("", status_code=403)


@router.post("/webhooks/wecom/kf", response_class=PlainTextResponse)
async def callback(
    request: Request,
    background: BackgroundTasks,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    crypto: CallbackCrypto = Depends(wecom_crypto),
    engine: AsyncEngine = Depends(db_engine),
    ring: Keyring = Depends(keyring),
    now: datetime = Depends(clock),
    client: WeComClient = Depends(wecom_client),
    settings: Settings = Depends(get_settings),
):
    body = await _bounded_body(request)
    if body is None:
        return PlainTextResponse("", status_code=413)
    try:
        message = crypto.decrypt_callback(msg_signature, timestamp, nonce, body, now=now.timestamp())
    except WeComCryptoError as exc:
        log.warning("wecom callback rejected: %s", exc)
        return PlainTextResponse("", status_code=403)

    if message.get("Event") == "kf_msg_or_event":
        open_kfid, token = message.get("OpenKfId", ""), message.get("Token", "")
        if settings.wecom_kf_open_kfid and open_kfid != settings.wecom_kf_open_kfid:
            log.warning("callback for unexpected open_kfid %s ignored", open_kfid)
            return PlainTextResponse("")
        if open_kfid and token:
            async with engine.begin() as conn:
                await record_callback_token(conn, open_kfid, token, now=now, keyring=ring)
            background.add_task(
                _pull,
                engine,
                client,
                corp_id=crypto.receive_id,
                open_kfid=open_kfid,
                now=now,
                keyring=ring,
            )
    return PlainTextResponse("")  # empty 200: received, no passive reply
