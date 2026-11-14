"""WeCom server API used by the KF integration (design doc §3, §7.1).

The access token comes from `gettoken` with the corp id and the self-built app's secret (the app authorised
to call 微信客服 APIs). It is cached until shortly before expiry and refreshed once when WeCom reports it
invalid. Every call runs from a machine whose egress IP is in the app's 企业可信IP (else errcode 60020).
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

BASE_URL = "https://qyapi.weixin.qq.com"
_TOKEN_SAFETY_MARGIN = 300  # refresh five minutes early
_INVALID_TOKEN_CODES = {40014, 42001, 42007, 42009}


class WeComApiError(Exception):
    def __init__(self, errcode: int, errmsg: str, api: str):
        super().__init__(f"{api}: {errcode} {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg
        self.api = api


MEDIA_CHUNK = 16 * 1024 * 1024  # <= 20 MB per Range request, a multiple of 16 bytes
MEDIA_EXPIRED = {40007, 40145}  # invalid / expired media_id (valid 3 days)


class MediaTooLarge(Exception):
    def __init__(self, size: int):
        super().__init__(f"media is {size} bytes")
        self.size = size


@dataclass(frozen=True)
class MediaFile:
    file_name: str | None  # display only; may be a placeholder like MEDIA_ID.ext
    content_type: str | None
    size: int


def _content_range_total(value: str) -> int | None:
    m = re.fullmatch(r"\s*bytes\s+\d+-\d+/(\d+)\s*", value)
    return int(m.group(1)) if m else None


def _disposition_name(value: str) -> str | None:
    """File name from Content-Disposition (RFC 6266 filename* first), cleaned for display."""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", value, re.I)
    if m:
        name = unquote(m.group(1).strip())
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', value, re.I)
        if not m:
            return None
        name = m.group(1).strip()
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name).strip()[:200]
    return name or None


@dataclass
class SyncPage:
    next_cursor: str
    has_more: bool
    msg_list: list[dict[str, Any]] = field(default_factory=list)


class WeComClient:
    def __init__(self, corp_id: str, app_secret: str, *, http: httpx.AsyncClient | None = None):
        self._corp_id = corp_id
        self._secret = app_secret
        self._http = http or httpx.AsyncClient(base_url=BASE_URL, timeout=10)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self, *, force: bool = False) -> str:
        async with self._lock:
            if force or self._token is None or time.monotonic() >= self._token_expires_at:
                resp = await self._call(
                    "gettoken",
                    self._http.get(
                        "/cgi-bin/gettoken", params={"corpid": self._corp_id, "corpsecret": self._secret}
                    ),
                )
                data = self._checked(resp, "gettoken")
                self._token = data["access_token"]
                self._token_expires_at = (
                    time.monotonic() + int(data.get("expires_in", 7200)) - _TOKEN_SAFETY_MARGIN
                )
            return self._token

    @staticmethod
    async def _call(api: str, request) -> httpx.Response:
        """Transport errors become WeComApiError(-1). The URL carries corpsecret or access_token, so no
        httpx exception (whose text includes the URL) may escape, not even as __context__."""
        try:
            return await request
        except httpx.HTTPError as exc:
            raise WeComApiError(-1, f"transport: {type(exc).__name__}", api) from None

    @staticmethod
    def _checked(resp: httpx.Response, api: str) -> dict[str, Any]:
        if resp.status_code != 200:
            raise WeComApiError(-1, f"HTTP {resp.status_code}", api)
        try:
            data = resp.json()
        except ValueError:
            raise WeComApiError(-1, "response is not JSON", api) from None
        if data.get("errcode", 0) != 0:
            raise WeComApiError(data["errcode"], data.get("errmsg", ""), api)
        return data

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in (1, 2):
            token = await self._access_token(force=attempt == 2)
            resp = await self._call(path, self._http.post(path, params={"access_token": token}, json=body))
            try:
                return self._checked(resp, path)
            except WeComApiError as exc:
                if exc.errcode in _INVALID_TOKEN_CODES and attempt == 1:
                    continue  # token revoked early: refresh once
                raise
        raise AssertionError("unreachable")

    # ------------------------------------------------------------ 素材

    async def download_media(self, media_id: str, dest: Path, *, max_bytes: int) -> MediaFile:
        """获取临时素材 (official doc 90254) into `dest`, in HTTP Range chunks: a large file must be fetched
        in pieces of at most 20 MB (else 830002), and encrypted resources need chunk lengths that are
        multiples of 16 bytes (else 40146). A media_id is valid for 3 days. Raises MediaTooLarge beyond
        max_bytes."""
        api = "media/get"
        start, total, info = 0, None, None
        with dest.open("wb") as out:
            while total is None or start < total:
                end = start + MEDIA_CHUNK - 1
                resp = await self._media_chunk(media_id, start, end)
                if resp.status_code == 206:
                    total = _content_range_total(resp.headers.get("content-range", ""))
                elif resp.status_code == 200 and start == 0:
                    total = len(resp.content)  # small file: the whole body, Range ignored
                else:
                    raise WeComApiError(-1, f"HTTP {resp.status_code}", api)
                if total is None:
                    raise WeComApiError(-1, "unreadable Content-Range", api)
                if total > max_bytes:
                    raise MediaTooLarge(total)
                if info is None:
                    info = MediaFile(
                        file_name=_disposition_name(resp.headers.get("content-disposition", "")),
                        content_type=resp.headers.get("content-type", "").split(";")[0].strip() or None,
                        size=total,
                    )
                out.write(resp.content)
                start += len(resp.content)
                if resp.status_code == 200 or not resp.content:
                    break
        if info is None or start != total:
            raise WeComApiError(-1, "incomplete download", api)
        return info

    async def media_head(self, media_id: str, size: int) -> tuple[bytes, MediaFile]:
        """The first `size` bytes (a multiple of 16) and what the headers say about the file, to tell its
        kind before anything is stored."""
        resp = await self._media_chunk(media_id, 0, size - 1)
        if resp.status_code == 206:
            total = _content_range_total(resp.headers.get("content-range", "")) or len(resp.content)
        elif resp.status_code == 200:
            total = len(resp.content)
        else:
            raise WeComApiError(-1, f"HTTP {resp.status_code}", "media/get")
        info = MediaFile(
            file_name=_disposition_name(resp.headers.get("content-disposition", "")),
            content_type=resp.headers.get("content-type", "").split(";")[0].strip() or None,
            size=total,
        )
        return resp.content[:size], info

    async def _media_chunk(self, media_id: str, start: int, end: int) -> httpx.Response:
        for attempt in (1, 2):
            token = await self._access_token(force=attempt == 2)
            resp = await self._call(
                "media/get",
                self._http.get(
                    "/cgi-bin/media/get",
                    params={"access_token": token, "media_id": media_id},
                    headers={"Range": f"bytes={start}-{end}"},
                ),
            )
            # Errors come back as a JSON body (the header set of the error case is not documented).
            if "content-disposition" not in resp.headers and resp.content[:1] == b"{":
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                errcode = data.get("errcode", 0)
                if errcode in _INVALID_TOKEN_CODES and attempt == 1:
                    continue
                if errcode:
                    raise WeComApiError(errcode, data.get("errmsg", ""), "media/get")
            return resp
        raise AssertionError("unreachable")

    # ------------------------------------------------------------ 微信客服

    async def sync_msg(
        self, *, open_kfid: str, cursor: str | None, token: str | None, limit: int = 1000
    ) -> SyncPage:
        """Without the callback token the call is heavily rate limited (official doc 94670)."""
        body: dict[str, Any] = {"open_kfid": open_kfid, "limit": limit}
        if cursor:
            body["cursor"] = cursor
        if token:
            body["token"] = token
        data = await self._post("/cgi-bin/kf/sync_msg", body)
        msg_list = data.get("msg_list")
        return SyncPage(
            next_cursor=data.get("next_cursor") or cursor or "",
            has_more=bool(data.get("has_more")),
            msg_list=msg_list if isinstance(msg_list, list) else [],
        )

    async def send_text(self, *, open_kfid: str, external_userid: str, content: str, msgid: str) -> str:
        """`msgid` (<= 32 chars of [0-9a-zA-Z_-], unique per KF account) makes retries detectable."""
        data = await self._post(
            "/cgi-bin/kf/send_msg",
            {
                "touser": external_userid,
                "open_kfid": open_kfid,
                "msgid": msgid,
                "msgtype": "text",
                "text": {"content": content},
            },
        )
        return data.get("msgid", msgid)

    async def send_welcome(self, *, code: str, content: str, msgid: str) -> None:
        """enter_session welcome: one message, within 20 seconds, code usable once (official doc 95122)."""
        await self._post(
            "/cgi-bin/kf/send_msg_on_event",
            {"code": code, "msgid": msgid, "msgtype": "text", "text": {"content": content}},
        )

    async def list_accounts(self) -> list[dict[str, Any]]:
        """客服账号列表 (official doc 94661): open_kfid and name of each account the app may use."""
        data = await self._post("/cgi-bin/kf/account/list", {"offset": 0, "limit": 100})
        return data.get("account_list", [])

    async def service_state(self, *, open_kfid: str, external_userid: str) -> int:
        data = await self._post(
            "/cgi-bin/kf/service_state/get", {"open_kfid": open_kfid, "external_userid": external_userid}
        )
        return int(data["service_state"])

    async def transfer_service_state(self, *, open_kfid: str, external_userid: str, state: int) -> None:
        await self._post(
            "/cgi-bin/kf/service_state/trans",
            {"open_kfid": open_kfid, "external_userid": external_userid, "service_state": state},
        )
