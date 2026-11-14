import json

import httpx
import pytest
from app.integrations.wecom.client import BASE_URL, WeComApiError, WeComClient


class FakeWeCom:
    """Records requests and answers like qyapi.weixin.qq.com."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.valid_token = None
        self.sync_pages: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/cgi-bin/gettoken":
            self.tokens_issued += 1
            self.valid_token = f"tok{self.tokens_issued}"
            return httpx.Response(
                200, json={"errcode": 0, "access_token": self.valid_token, "expires_in": 7200}
            )
        if request.url.params.get("access_token") != self.valid_token:
            return httpx.Response(200, json={"errcode": 42001, "errmsg": "access_token expired"})
        if request.url.path == "/cgi-bin/kf/sync_msg":
            return httpx.Response(200, json={"errcode": 0, **self.sync_pages.pop(0)})
        if request.url.path == "/cgi-bin/kf/send_msg":
            body = json.loads(request.content)
            return httpx.Response(200, json={"errcode": 0, "msgid": body["msgid"]})
        if request.url.path == "/cgi-bin/kf/service_state/get":
            return httpx.Response(200, json={"errcode": 0, "service_state": 1})
        return httpx.Response(200, json={"errcode": 0})


@pytest.fixture
def fake():
    return FakeWeCom()


@pytest.fixture
async def client(fake):
    http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(fake.handler))
    c = WeComClient("corp", "secret", http=http)
    yield c
    await c.aclose()


async def test_token_is_cached(client, fake):
    fake.sync_pages = [{"next_cursor": "c1", "has_more": 0, "msg_list": []}] * 2
    await client.sync_msg(open_kfid="kf", cursor=None, token="t")
    await client.sync_msg(open_kfid="kf", cursor="c1", token=None)
    assert fake.tokens_issued == 1


async def test_invalid_token_is_refreshed_once(client, fake):
    fake.sync_pages = [{"next_cursor": "c1", "has_more": 0, "msg_list": []}]
    await client._access_token()
    fake.valid_token = "rotated-by-someone-else"  # WeCom invalidated our cached token
    fake.tokens_issued = 10
    page = await client.sync_msg(open_kfid="kf", cursor=None, token=None)
    assert page.next_cursor == "c1"
    assert fake.tokens_issued == 11


async def test_sync_request_body(client, fake):
    fake.sync_pages = [{"next_cursor": "c2", "has_more": 1, "msg_list": [{"msgid": "m1"}]}]
    page = await client.sync_msg(open_kfid="kf", cursor="c1", token="cb")
    body = json.loads(fake.requests[-1].content)
    assert body == {"open_kfid": "kf", "limit": 1000, "cursor": "c1", "token": "cb"}
    assert (page.next_cursor, page.has_more, page.msg_list) == ("c2", True, [{"msgid": "m1"}])


async def test_send_text_passes_msgid(client, fake):
    msgid = await client.send_text(open_kfid="kf", external_userid="wm1", content="hi", msgid="a" * 32)
    body = json.loads(fake.requests[-1].content)
    assert body["msgid"] == "a" * 32 and body["text"] == {"content": "hi"} and msgid == "a" * 32


async def test_api_error_is_raised(fake):
    def failing(request):
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "t", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 60020, "errmsg": "not allow to access from your ip"})

    http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(failing))
    c = WeComClient("corp", "secret", http=http)
    with pytest.raises(WeComApiError) as info:
        await c.service_state(open_kfid="kf", external_userid="wm1")
    assert info.value.errcode == 60020
    await c.aclose()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(502, text="bad gateway"), "HTTP 502"),
        (httpx.Response(200, text="<html>"), "response is not JSON"),
        (httpx.ReadTimeout("timed out"), "transport: ReadTimeout"),
    ],
)
async def test_http_failures_never_carry_the_secret(response, message):
    """Errors become WeComApiError(-1) without the URL, which holds corpsecret / access_token."""

    def handler(request):
        if isinstance(response, Exception):
            raise response
        return response

    http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    c = WeComClient("corp", "TOPSECRET", http=http)
    with pytest.raises(WeComApiError) as info:
        await c.service_state(open_kfid="kf", external_userid="wm1")
    assert (info.value.errcode, info.value.errmsg) == (-1, message)
    assert "TOPSECRET" not in str(info.value)
    assert info.value.__context__ is None or info.value.__suppress_context__
    await c.aclose()


def test_httpx_request_logging_is_off():
    import logging

    import app.main  # noqa: F401  (configures the loggers)

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


class MediaServer:
    def __init__(self, data: bytes, *, ranges=True, disposition='attachment; filename="MEDIA.m4a"'):
        self.data = data
        self.ranges = ranges
        self.disposition = disposition
        self.requested: list[str] = []
        self.errors: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "t", "expires_in": 7200})
        assert request.url.path == "/cgi-bin/media/get"
        if self.errors:
            return httpx.Response(200, json=self.errors.pop(0))
        rng = request.headers["range"]
        self.requested.append(rng)
        headers = {"content-type": "audio/mp4", "content-disposition": self.disposition}
        if not self.ranges:
            return httpx.Response(200, content=self.data, headers=headers)
        start, end = map(int, rng.removeprefix("bytes=").split("-"))
        chunk = self.data[start : end + 1]
        headers["content-range"] = f"bytes {start}-{start + len(chunk) - 1}/{len(self.data)}"
        return httpx.Response(206, content=chunk, headers=headers)


def media_client(server):
    http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(server.handler))
    return WeComClient("corp", "secret", http=http)


async def test_media_download_in_aligned_chunks(tmp_path, monkeypatch):
    from app.integrations.wecom import client as wc

    monkeypatch.setattr(wc, "MEDIA_CHUNK", 32)
    data = bytes(range(256)) * 3  # 768 bytes -> 24 chunks
    server = MediaServer(data[:700])
    info = await media_client(server).download_media("m1", tmp_path / "f", max_bytes=10_000)
    assert (tmp_path / "f").read_bytes() == data[:700]
    assert (info.file_name, info.content_type, info.size) == ("MEDIA.m4a", "audio/mp4", 700)
    assert server.requested[:2] == ["bytes=0-31", "bytes=32-63"]
    assert all((int(r.split("-")[0].removeprefix("bytes=")) % 16) == 0 for r in server.requested)


async def test_small_media_without_range_support(tmp_path):
    server = MediaServer(b"tiny", ranges=False)
    info = await media_client(server).download_media("m1", tmp_path / "f", max_bytes=100)
    assert (tmp_path / "f").read_bytes() == b"tiny" and info.size == 4


async def test_media_too_large_stops_at_the_first_chunk(tmp_path, monkeypatch):
    from app.integrations.wecom import client as wc

    monkeypatch.setattr(wc, "MEDIA_CHUNK", 16)
    server = MediaServer(b"x" * 1000)
    with pytest.raises(wc.MediaTooLarge):
        await media_client(server).download_media("m1", tmp_path / "f", max_bytes=999)
    assert len(server.requested) == 1


async def test_expired_media_and_token_refresh(tmp_path):
    server = MediaServer(b"data")
    server.errors = [
        {"errcode": 42001, "errmsg": "token expired"},
        {"errcode": 40007, "errmsg": "invalid media_id"},
    ]
    with pytest.raises(WeComApiError) as info:
        await media_client(server).download_media("m1", tmp_path / "f", max_bytes=100)
    assert info.value.errcode == 40007


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ("attachment; filename*=UTF-8''%E4%BC%9A%E8%AE%AE%E5%BD%95%E9%9F%B3.m4a", "会议录音.m4a"),
        ('attachment; filename="../../etc/passwd"', ".._.._etc_passwd"),
        ("attachment", None),
    ],
)
async def test_media_file_name_is_display_only(tmp_path, header, name):
    server = MediaServer(b"data", disposition=header)
    info = await media_client(server).download_media("m1", tmp_path / "f", max_bytes=100)
    assert info.file_name == name


async def test_media_head(tmp_path):
    server = MediaServer(b"\x00\x00\x00\x20ftypM4A " + b"x" * 10_000)
    head, info = await media_client(server).media_head("m1", 4096)
    assert len(head) == 4096 and head[4:8] == b"ftyp" and info.size == 10_012
    assert server.requested == ["bytes=0-4095"]
