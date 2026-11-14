"""Tencent Cloud COS (XML API) for meeting audio: presigned PUT/GET, HEAD, delete, prefix cleanup, and file
transfer for the merge step (design doc §7.5, official doc 436/7778 for request signing).

Signing: HttpString = "method\\npath\\nparams\\nheaders\\n" with the decoded path; keys lowercased, keys and
values URL-encoded (only A-Z a-z 0-9 - _ . ~ left as is) and sorted. tests/unit/test_cos.py checks this
against the SHA1 values published in the official examples.

A presigned PUT signs `content-length` and `x-cos-meta-sha256`, so COS rejects an upload whose size or
declared hash differs from what the Worker registered; `complete` then reads both back with HEAD.
"""

import asyncio
import hashlib
import hmac
import time
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterable, Callable, Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from ..services.uploads import ObjectStat, UploadTarget

SHA256_META = "x-cos-meta-sha256"
_IO_BLOCK = 1 << 20
_CLOCK_SKEW = 60  # KeyTime starts a minute early so a slightly fast COS clock still accepts it


def _enc(value: str) -> str:
    return quote(value, safe="-_.~")


def _canonical(pairs: dict[str, str]) -> tuple[str, str]:
    """(key list, key=value string) as COS signs them."""
    items = sorted((_enc(k).lower(), _enc(v)) for k, v in pairs.items())
    return ";".join(k for k, _ in items), "&".join(f"{k}={v}" for k, v in items)


def http_string(method: str, path: str, params: dict[str, str], headers: dict[str, str]) -> str:
    _, http_params = _canonical(params)
    _, http_headers = _canonical(headers)
    return f"{method.lower()}\n{path}\n{http_params}\n{http_headers}\n"


def sign(
    *,
    secret_id: str,
    secret_key: str,
    method: str,
    path: str,
    params: dict[str, str],
    headers: dict[str, str],
    start: int,
    end: int,
) -> str:
    """The Authorization value (also usable as a query string for presigned URLs)."""
    key_time = f"{start};{end}"
    sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
    digest = hashlib.sha1(http_string(method, path, params, headers).encode()).hexdigest()
    string_to_sign = f"sha1\n{key_time}\n{digest}\n"
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()
    header_list, _ = _canonical(headers)
    param_list, _ = _canonical(params)
    return (
        f"q-sign-algorithm=sha1&q-ak={secret_id}&q-sign-time={key_time}&q-key-time={key_time}"
        f"&q-header-list={header_list}&q-url-param-list={param_list}&q-signature={signature}"
    )


class CosError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(f"COS HTTP {status} {code}")
        self.status = status
        self.code = code


class CosStorage:
    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        secret_id: str,
        secret_key: str,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        request_ttl: int = 600,
    ):
        self.host = f"{bucket}.cos.{region}.myqcloud.com"
        self._id = secret_id
        self._key = secret_key
        self._http = http or httpx.AsyncClient(base_url=f"https://{self.host}", timeout=60)
        self._clock = clock
        self._ttl = request_ttl

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ signing helpers

    @staticmethod
    def _path(object_key: str) -> str:
        if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
            raise ValueError("invalid object key")
        return "/" + object_key

    def _auth(self, method: str, path: str, params: dict[str, str], headers: dict[str, str], end: int) -> str:
        return sign(
            secret_id=self._id,
            secret_key=self._key,
            method=method,
            path=path,
            params=params,
            headers=headers,
            start=int(self._clock()) - _CLOCK_SKEW,
            end=end,
        )

    def _url(self, path: str, query: str) -> str:
        return f"https://{self.host}{quote(path, safe='/-_.~')}?{query}"

    async def _request(
        self,
        method: str,
        object_key: str | None,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | Iterable[bytes] | AsyncIterable[bytes] | None = None,
        ok: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        path = self._path(object_key) if object_key is not None else "/"
        params = params or {}
        signed = {"host": self.host, **(headers or {})}
        auth = self._auth(method, path, params, signed, int(self._clock()) + self._ttl)
        resp = await self._http.request(
            method,
            quote(path, safe="/-_.~"),
            params=params,
            headers={**signed, "Authorization": auth},
            content=content,
        )
        if resp.status_code not in ok:
            raise CosError(resp.status_code, _error_code(resp))
        return resp

    # ------------------------------------------------------------ ObjectStorage

    async def presign_put(
        self, object_key: str, *, size: int, sha256: str, expires_at: datetime
    ) -> UploadTarget:
        path = self._path(object_key)
        headers = {"host": self.host, "content-length": str(size), SHA256_META: sha256}
        auth = self._auth("put", path, {}, headers, int(expires_at.timestamp()))
        return UploadTarget(
            object_key=object_key,
            url=self._url(path, _query(auth)),
            expires_at=expires_at,
            headers={SHA256_META: sha256},
        )

    async def presign_get(self, object_key: str, *, expires_at: datetime) -> str:
        path = self._path(object_key)
        auth = self._auth("get", path, {}, {"host": self.host}, int(expires_at.timestamp()))
        return self._url(path, _query(auth))

    async def stat(self, object_key: str) -> ObjectStat | None:
        resp = await self._request("HEAD", object_key, ok=(200, 404))
        if resp.status_code == 404:
            return None
        return ObjectStat(bytes=int(resp.headers["content-length"]), sha256=resp.headers.get(SHA256_META))

    async def delete(self, object_key: str) -> None:
        await self._request("DELETE", object_key, ok=(200, 204, 404))

    async def delete_prefix(self, prefix: str) -> None:
        if not prefix.endswith("/") or prefix.startswith("/"):
            raise ValueError("prefix must be a directory like tasks/<id>/audio/")
        marker = ""
        while True:
            params = {"prefix": prefix, "marker": marker, "max-keys": "1000"}
            resp = await self._request("GET", None, params=params)
            root = _xml(resp.content, "ListBucketResult")
            keys = [el.text for el in root.iter("Key") if el.text]
            for key in keys:
                await self.delete(key)
            if root.findtext("IsTruncated") != "true" or not keys:
                break
            marker = root.findtext("NextMarker") or keys[-1]
        await self._abort_uploads(prefix)

    async def _abort_uploads(self, prefix: str) -> None:
        """Unfinished multipart uploads are invisible to listing but still hold data (and could complete)."""
        key_marker = upload_marker = ""
        while True:
            params = {
                "uploads": "",
                "prefix": prefix,
                "key-marker": key_marker,
                "upload-id-marker": upload_marker,
            }
            resp = await self._request("GET", None, params=params)
            root = _xml(resp.content, "ListMultipartUploadsResult")
            for upload in root.iter("Upload"):
                key, upload_id = upload.findtext("Key"), upload.findtext("UploadId")
                if key and upload_id:
                    await self._request("DELETE", key, params={"uploadId": upload_id}, ok=(200, 204, 404))
            if root.findtext("IsTruncated") != "true":
                break
            key_marker = root.findtext("NextKeyMarker") or ""
            upload_marker = root.findtext("NextUploadIdMarker") or ""

    # ------------------------------------------------------------ BlobStore (merge step)

    async def download_file(self, object_key: str, path: Path) -> None:
        url_path = quote(self._path(object_key), safe="/-_.~")
        headers = {"host": self.host}
        auth = self._auth("GET", self._path(object_key), {}, headers, int(self._clock()) + self._ttl)
        async with self._http.stream("GET", url_path, headers={**headers, "Authorization": auth}) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise CosError(resp.status_code, _error_code(resp))
            with path.open("wb") as out:
                async for chunk in resp.aiter_bytes(1 << 20):
                    out.write(chunk)

    async def upload_file(self, path: Path, object_key: str, *, content_type: str) -> None:
        """Single PUT (COS allows up to 5 GB), streamed from disk: an uploaded source file can be 2 GB."""
        sha = await asyncio.to_thread(_sha256_file, path)  # 2 GB of hashing must not stall the event loop
        size = path.stat().st_size

        async def body():
            with path.open("rb") as f:
                while block := await asyncio.to_thread(f.read, _IO_BLOCK):
                    yield block

        await self._request(
            "PUT",
            object_key,
            headers={
                "content-type": content_type,
                "content-length": str(size),
                SHA256_META: sha,
            },
            content=body(),
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(_IO_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _query(authorization: str) -> str:
    """Presigned form: every q-* field as a URL-encoded query parameter."""
    return "&".join(f"{k}={_enc(v)}" for k, v in (part.split("=", 1) for part in authorization.split("&")))


def _xml(content: bytes, root: str | None = None) -> ET.Element:
    """A response document with namespaces dropped. S3-style services may or may not qualify their tags, and
    a lookup by bare name in a qualified document finds nothing: a listing would look empty and a deletion
    would be reported done with every object still there. An unexpected document is an error, never empty."""
    tree = ET.fromstring(content)
    for el in tree.iter():
        if isinstance(el.tag, str):
            el.tag = el.tag.rpartition("}")[2]
    if root is not None and tree.tag != root:
        raise CosError(200, f"unexpected {tree.tag[:40]}")
    return tree


def _error_code(resp: httpx.Response) -> str:
    try:
        return _xml(resp.content).findtext("Code") or ""
    except ET.ParseError:
        return ""
