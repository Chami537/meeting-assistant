"""COS request signing (official doc 436/7778 examples) and the storage client against a fake bucket."""

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx
import pytest
from app.integrations import cos

HOST = "examplebucket-1250000000.cos.ap-beijing.myqcloud.com"


def test_http_string_matches_official_put_example():
    headers = {
        "Content-Length": "13",
        "Content-MD5": "mQ/fVh815F3k6TAUm8m0eg==",
        "Content-Type": "text/plain",
        "Date": "Thu, 16 May 2019 06:45:51 GMT",
        "Host": HOST,
        "x-cos-acl": "private",
        "x-cos-grant-read": 'uin="100000000011"',
    }
    s = cos.http_string("PUT", "/exampleobject(腾讯云)", {}, headers)
    assert hashlib.sha1(s.encode()).hexdigest() == "8b2751e77f43a0995d6e9eb9477f4b685cca4172"


def test_http_string_matches_official_get_example():
    params = {"response-content-type": "application/octet-stream", "response-cache-control": "max-age=600"}
    headers = {"Date": "Thu, 16 May 2019 06:55:53 GMT", "Host": HOST}
    s = cos.http_string("GET", "/exampleobject(腾讯云)", params, headers)
    assert hashlib.sha1(s.encode()).hexdigest() == "54ecfe22f59d3514fdc764b87a32d8133ea611e6"


def test_signature_uses_hex_sign_key():
    auth = cos.sign(
        secret_id="AKID",
        secret_key="SK",
        method="get",
        path="/a",
        params={},
        headers={"host": HOST},
        start=1,
        end=2,
    )
    fields = dict(part.split("=", 1) for part in auth.split("&"))
    assert fields["q-sign-time"] == fields["q-key-time"] == "1;2"
    assert (fields["q-header-list"], fields["q-url-param-list"]) == ("host", "")
    sign_key = hmac.new(b"SK", b"1;2", hashlib.sha1).hexdigest()
    digest = hashlib.sha1(cos.http_string("get", "/a", {}, {"host": HOST}).encode()).hexdigest()
    expected = hmac.new(sign_key.encode(), f"sha1\n1;2\n{digest}\n".encode(), hashlib.sha1).hexdigest()
    assert fields["q-signature"] == expected


class FakeBucket:
    """Checks every request's signature with the shared secret, like COS would."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, dict]] = {}
        self.uploads = [("tasks/t/audio/half.m4a", "u1")]
        self.aborted = []
        self.page_size = 2
        self.xmlns = ""  # e.g. ' xmlns="http://s3.amazonaws.com/doc/2006-03-01/"' for a qualified document

    def verify(self, request: httpx.Request, auth: str):
        fields = dict(part.split("=", 1) for part in auth.split("&"))
        params = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        params = {k: v for k, v in params.items() if not k.startswith("q-")}
        names = fields["q-header-list"].split(";") if fields["q-header-list"] else []
        headers = {n: request.headers[n] for n in names}
        start, end = map(int, fields["q-key-time"].split(";"))
        expected = cos.sign(
            secret_id="AKID",
            secret_key="SK",
            method=request.method,
            path=unquote(request.url.path),
            params=params,
            headers=headers,
            start=start,
            end=end,
        )
        assert auth == expected

    def handler(self, request: httpx.Request):
        auth = request.headers.get("authorization")
        if auth is None:  # presigned
            query = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
            auth = "&".join(
                f"{k}={query[k]}"
                for k in (
                    "q-sign-algorithm",
                    "q-ak",
                    "q-sign-time",
                    "q-key-time",
                    "q-header-list",
                    "q-url-param-list",
                    "q-signature",
                )
            )
        self.verify(request, auth)
        key = unquote(request.url.path)[1:]
        params = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        if request.method == "GET" and key == "" and "uploads" in params:
            body = "".join(
                f"<Upload><Key>{k}</Key><UploadId>{u}</UploadId></Upload>" for k, u in self.uploads
            )
            return httpx.Response(
                200,
                content=f"<ListMultipartUploadsResult{self.xmlns}>{body}<IsTruncated>false</IsTruncated></ListMultipartUploadsResult>",
            )
        if request.method == "GET" and key == "":
            keys = sorted(k for k in self.objects if k.startswith(params["prefix"]) and k > params["marker"])
            page = keys[: self.page_size]
            truncated = "true" if len(keys) > self.page_size else "false"
            contents = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in page)
            return httpx.Response(
                200,
                content=f"<ListBucketResult{self.xmlns}>{contents}<IsTruncated>{truncated}</IsTruncated></ListBucketResult>",
            )
        if request.method == "DELETE" and "uploadId" in params:
            self.aborted.append((key, params["uploadId"]))
            return httpx.Response(204)
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        if request.method == "PUT":
            self.objects[key] = (request.content, dict(request.headers))
            return httpx.Response(200)
        if request.method in ("GET", "HEAD"):
            if key not in self.objects:
                return httpx.Response(404, content=b"<Error><Code>NoSuchKey</Code></Error>")
            data, headers = self.objects[key]
            extra = {cos.SHA256_META: headers[cos.SHA256_META]} if cos.SHA256_META in headers else {}
            return httpx.Response(
                200,
                content=data if request.method == "GET" else b"",
                headers={"content-length": str(len(data)), **extra},
            )
        return httpx.Response(400)


@pytest.fixture
def bucket():
    fake = FakeBucket()
    http = httpx.AsyncClient(base_url=f"https://{HOST}", transport=httpx.MockTransport(fake.handler))
    storage = cos.CosStorage(
        bucket="examplebucket-1250000000",
        region="ap-beijing",
        secret_id="AKID",
        secret_key="SK",
        http=http,
        clock=lambda: 1_700_000_000,
    )
    return fake, storage, http


async def test_presigned_put_signs_size_and_hash(bucket):
    fake, storage, http = bucket
    expires = datetime.fromtimestamp(1_700_001_800, UTC)
    target = await storage.presign_put("tasks/t/audio/0.m4a", size=5, sha256="ab" * 32, expires_at=expires)
    assert target.headers == {cos.SHA256_META: "ab" * 32}
    url = urlsplit(target.url)
    assert url.netloc == HOST and url.path == "/tasks/t/audio/0.m4a"
    query = dict(parse_qsl(url.query))
    assert query["q-key-time"] == "1699999940;1700001800"
    assert query["q-header-list"] == f"content-length;host;{cos.SHA256_META}"
    resp = await http.put(target.url, content=b"hello", headers=target.headers)
    assert resp.status_code == 200
    with pytest.raises(AssertionError):  # a different size breaks the signature
        await http.put(target.url, content=b"hello!", headers=target.headers)
    assert await storage.stat("tasks/t/audio/0.m4a") == cos.ObjectStat(bytes=5, sha256="ab" * 32)
    assert await storage.stat("tasks/t/audio/missing") is None


async def test_presigned_get(bucket):
    fake, storage, http = bucket
    fake.objects["tasks/t/audio/m.m4a"] = (b"data", {})
    url = await storage.presign_get(
        "tasks/t/audio/m.m4a", expires_at=datetime.fromtimestamp(1_700_043_200, UTC)
    )
    resp = await http.get(url)
    assert resp.content == b"data"


async def test_delete_prefix_pages_and_aborts_multipart(bucket):
    fake, storage, _ = bucket
    for name in ("a", "b", "c", "d", "e"):
        fake.objects[f"tasks/t/audio/{name}"] = (b"x", {})
    fake.objects["tasks/other/audio/a"] = (b"x", {})
    await storage.delete_prefix("tasks/t/audio/")
    assert list(fake.objects) == ["tasks/other/audio/a"]
    assert fake.aborted == [("tasks/t/audio/half.m4a", "u1")]
    with pytest.raises(ValueError):
        await storage.delete_prefix("tasks/t")  # not a directory: would also match tasks/t2/...


async def test_delete_prefix_reads_namespaced_listings(bucket):
    """A qualified document (S3 style) must not look empty: that would report a deletion that never ran."""
    fake, storage, _ = bucket
    fake.xmlns = ' xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'
    for name in ("a", "b", "c"):
        fake.objects[f"tasks/t/audio/{name}"] = (b"x", {})
    await storage.delete_prefix("tasks/t/audio/")
    assert list(fake.objects) == []
    assert fake.aborted == [("tasks/t/audio/half.m4a", "u1")]


async def test_delete_prefix_refuses_an_unexpected_listing():
    def handler(request):
        return httpx.Response(200, content=b"<Html><Key>tasks/t/audio/a</Key></Html>")

    http = httpx.AsyncClient(base_url=f"https://{HOST}", transport=httpx.MockTransport(handler))
    storage = cos.CosStorage(
        bucket="examplebucket-1250000000", region="ap-beijing", secret_id="AKID", secret_key="SK", http=http
    )
    with pytest.raises(cos.CosError):
        await storage.delete_prefix("tasks/t/audio/")


async def test_file_round_trip(bucket, tmp_path):
    fake, storage, _ = bucket
    src = tmp_path / "in.m4a"
    src.write_bytes(b"merged audio")
    await storage.upload_file(src, "tasks/t/audio/merged-v1.m4a", content_type="audio/mp4")
    data, headers = fake.objects["tasks/t/audio/merged-v1.m4a"]
    assert data == b"merged audio"
    assert headers[cos.SHA256_META] == hashlib.sha256(b"merged audio").hexdigest()
    out = tmp_path / "out.m4a"
    await storage.download_file("tasks/t/audio/merged-v1.m4a", out)
    assert out.read_bytes() == b"merged audio"
    with pytest.raises(cos.CosError) as exc:
        await storage.download_file("tasks/t/audio/none", out)
    assert (exc.value.status, exc.value.code) == (404, "NoSuchKey")


@pytest.mark.parametrize("key", ["", "/abs", "tasks/../secrets", "tasks/t/../../x"])
async def test_rejects_odd_keys(bucket, key):
    _, storage, _ = bucket
    with pytest.raises(ValueError):
        await storage.stat(key)
