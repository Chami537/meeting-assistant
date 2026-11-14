"""Upload page for recordings too large for WeChat KF (files above 20 MB, videos above 10 MB; design doc §4).

    GET /u/{token}   the declaration, a checkbox and a file picker; consumes nothing
    PUT /u/{token}   the raw file body (sent by the page's script with a progress bar); headers
                     X-Consent-Version (the declaration the user ticked) and X-File-Name (display only)

The body is streamed to a temporary file with a hard cap, never held in memory. The only script on the page
is allowed by its hash (api/pages.py).
"""

import asyncio
import html
import logging
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, JSONResponse
from meeting_protocol import TaskStatus
from sqlalchemy.ext.asyncio import AsyncEngine

from ..deps import clock, db_engine, keyring, object_storage
from ..intake import material
from ..intake import messages as msgs
from ..security.keyring import Keyring
from ..services import results
from ..services.results import TOKEN_PATTERN
from ..services.uploads import ObjectStorage
from ..services.user_uploads import MAX_SOURCE_BYTES, UploadError, accept_web_upload
from .pages import page, same_origin

log = logging.getLogger(__name__)
router = APIRouter(prefix="/u", include_in_schema=False)
Token = Annotated[str, PathParam(pattern=TOKEN_PATTERN)]

EXPIRED = "链接已失效。请在微信里回复「上传录音」获取新的链接。"
RECEIVED = "这个链接已经上传过录音，正在处理中，纪要生成后会发到微信。"
MAX_CONCURRENT_UPLOADS = 3  # each one streams to the temporary disk
MIN_FREE_DISK = 1024**3
RECEIVE_TIMEOUT_S = 2 * 3600

_UPLOADS = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
_IN_FLIGHT: dict[UUID, int] = {}  # task id -> declared size, for uploads in progress in this process
IDLE_TIMEOUT_S = 60

# Kept byte-for-byte stable: the Content-Security-Policy allows exactly this script by its hash.
SCRIPT = """
(function () {
  var form = document.getElementById('f'), msg = document.getElementById('msg');
  var go = document.getElementById('go'), agree = document.getElementById('agree');
  var max = parseInt(form.getAttribute('data-max'), 10);
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var file = document.getElementById('file').files[0];
    if (!agree.checked) { msg.textContent = '请先勾选上面的确认事项。'; return; }
    if (!file) { msg.textContent = '请选择会议录音文件。'; return; }
    if (file.size > max) { msg.textContent = '文件超过大小上限。'; return; }
    var xhr = new XMLHttpRequest();
    xhr.open('PUT', window.location.pathname);
    xhr.setRequestHeader('X-Consent-Version', agree.value);
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name));
    xhr.upload.onprogress = function (ev) {
      if (ev.lengthComputable) { msg.textContent = '上传中 ' + Math.floor(ev.loaded * 100 / ev.total) + '%'; }
    };
    xhr.onload = function () {
      var body = {};
      try { body = JSON.parse(xhr.responseText); } catch (err) {}
      if (xhr.status === 200) { form.remove(); msg.textContent = body.message; return; }
      msg.textContent = body.error || '上传失败，请重试。';
      go.disabled = false;
    };
    xhr.onerror = function () { msg.textContent = '网络错误，请重试。'; go.disabled = false; };
    go.disabled = true;
    msg.textContent = '开始上传…';
    xhr.send(file);
  });
})();
"""


@router.get("/{token}")
async def upload_form(
    token: Token,
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
) -> HTMLResponse:
    async with engine.connect() as conn:
        slot = await results.upload_slot(conn, token, now=now)
        received = None if slot is not None else await results.upload_received(conn, token)
    if received is not None:
        return page("上传会议录音", f"<p>{_received_text(received)}</p>")
    if slot is None:
        return page("上传会议录音", f"<p>{EXPIRED}</p>", status=410)
    declaration = html.escape(msgs.UPLOAD_CONSENT_TEXT).replace("\n", "<br>")
    body = (
        "<h1>上传会议录音</h1>"
        f'<p class="note">{declaration}</p>'
        f'<form id="f" data-max="{MAX_SOURCE_BYTES}">'
        f'<p><label><input type="checkbox" id="agree" value="{msgs.UPLOAD_CONSENT_VERSION}"> '
        "我确认以上事项</label></p>"
        '<p><input type="file" id="file" accept="audio/*,video/*,.m4a,.mp3,.wav,.aac,.amr,.mp4"></p>'
        '<p><button id="go" type="submit">同意并上传</button></p></form>'
        f'<p id="msg" class="muted">{msgs.UPLOAD_FORMATS}</p>'
    )
    return page("上传会议录音", body, script=SCRIPT)


@router.put("/{token}")
async def upload_file(
    token: Token,
    request: Request,
    engine: AsyncEngine = Depends(db_engine),
    now: datetime = Depends(clock),
    ring: Keyring = Depends(keyring),
    storage: ObjectStorage = Depends(object_storage),
) -> JSONResponse:
    # Everything that can be decided from the headers is decided before a single body byte is read.
    if not same_origin(request):
        return _error(403, "请求来源不正确。")
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdigit():
        return _error(411, "请使用上传页面上传。")
    size = int(declared)
    if size > MAX_SOURCE_BYTES:
        return _error(413, "文件超过大小上限。")
    if size == 0:
        return _error(400, "文件是空的。")
    if request.headers.get("x-consent-version") != msgs.UPLOAD_CONSENT_VERSION:
        return _error(422, "请刷新页面，确认最新的说明后再上传。")
    async with engine.connect() as conn:
        slot = await results.upload_slot(conn, token, now=now)
        if slot is None:
            received = await results.upload_received(conn, token)
            if received is not None:
                return _error(409, _received_text(received))
            return _error(410, EXPIRED)
    if slot.task_id in _IN_FLIGHT:  # one upload per task at a time, whichever of its links is used
        return _error(409, "这个任务正在上传，请等待完成。")
    if _UPLOADS.locked():
        return _error(503, "当前上传人数较多，请稍后重试。")
    if shutil.disk_usage(tempfile.gettempdir()).free < size + _reserved() + MIN_FREE_DISK:
        log.error("upload refused: not enough temporary disk space")
        return _error(503, "服务器暂时无法接收，请稍后重试。")

    started = time.monotonic()
    file_name = material.display_name(unquote(request.headers.get("x-file-name", ""), errors="replace"))
    _IN_FLIGHT[slot.task_id] = size  # reserves its disk space too
    try:
        async with _UPLOADS:
            with tempfile.TemporaryDirectory(prefix="upload-") as tmp:
                source = Path(tmp) / "source"
                try:
                    async with asyncio.timeout(RECEIVE_TIMEOUT_S):
                        received = await _receive(request, source)
                except TimeoutError:
                    return _error(408, "上传时间过长，请在网络较好时重试。")
                if received is None:
                    return _error(413, "文件超过大小上限。")
                await accept_web_upload(
                    engine,
                    storage,
                    ring,
                    token,
                    source,
                    file_name=file_name,
                    consent_version=msgs.UPLOAD_CONSENT_VERSION,
                    # The transfer can take long; the commit is judged by the time it happens.
                    clock=lambda: now + timedelta(seconds=time.monotonic() - started),
                )
    except UploadError as exc:
        return _error(422, str(exc))
    except Exception as exc:
        log.error("upload failed: %s", type(exc).__name__)
        return _error(500, "上传失败，请重试。")
    finally:
        _IN_FLIGHT.pop(slot.task_id, None)
    return JSONResponse({"message": "上传成功。可以回到微信，纪要生成后会发给你。"}, headers=_NO_STORE)


async def _receive(request: Request, dest: Path) -> int | None:
    """Stream the body to disk; None once it passes MAX_SOURCE_BYTES (whatever Content-Length said). A
    sender that stops sending for IDLE_TIMEOUT_S is cut off, so it cannot hold an upload slot."""
    received = 0
    chunks = request.stream().__aiter__()
    with dest.open("wb") as out:
        while True:
            try:
                async with asyncio.timeout(IDLE_TIMEOUT_S):
                    chunk = await chunks.__anext__()
            except StopAsyncIteration:
                break
            received += len(chunk)
            if received > MAX_SOURCE_BYTES:
                return None
            await asyncio.to_thread(out.write, chunk)
    return received


def _reserved() -> int:
    return sum(_IN_FLIGHT.values())


def _received_text(status: TaskStatus) -> str:
    if status in (TaskStatus.UPLOADING, TaskStatus.TRANSCRIBING, TaskStatus.SUMMARIZING):
        return RECEIVED
    if status == TaskStatus.COMPLETED:
        return "这个链接上传的录音已经处理完成，纪要链接已发到微信；需要时可回复「查看纪要」。"
    return "这个链接已经用过。如需重新上传，请在微信里回复「上传录音」。"


_NO_STORE = {"Cache-Control": "no-store"}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message}, headers=_NO_STORE)
