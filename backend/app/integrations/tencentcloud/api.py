"""Tencent Cloud API 3.0 calls signed with TC3-HMAC-SHA256 (official doc 213/30654).

Only what the ASR client needs: POST with a JSON body, signed headers content-type, host and x-tc-action.
The date in the credential scope is the UTC date of the timestamp.
"""

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

ALGORITHM = "TC3-HMAC-SHA256"
CONTENT_TYPE = "application/json; charset=utf-8"
SIGNED_HEADERS = "content-type;host;x-tc-action"


class TencentCloudError(Exception):
    """An Error in the Response body; `code` is the documented code, e.g. FailedOperation.NoSuchTask."""

    def __init__(self, code: str, message: str, request_id: str | None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.request_id = request_id


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def canonical_request(host: str, action: str, payload: bytes) -> str:
    headers = f"content-type:{CONTENT_TYPE}\nhost:{host}\nx-tc-action:{action.lower()}\n"
    return "\n".join(["POST", "/", "", headers, SIGNED_HEADERS, _sha256_hex(payload)])


def authorization(
    *, secret_id: str, secret_key: str, service: str, host: str, action: str, payload: bytes, timestamp: int
) -> str:
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    scope = f"{date}/{service}/tc3_request"
    hashed = _sha256_hex(canonical_request(host, action, payload).encode())
    string_to_sign = f"{ALGORITHM}\n{timestamp}\n{scope}\n{hashed}"
    key = _hmac(_hmac(_hmac(f"TC3{secret_key}".encode(), date), service), "tc3_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"{ALGORITHM} Credential={secret_id}/{scope}, SignedHeaders={SIGNED_HEADERS}, Signature={signature}"
    )


class TencentCloudApi:
    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        *,
        service: str,
        version: str,
        region: str | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._id = secret_id
        self._key = secret_key
        self._service = service
        self._version = version
        self._region = region
        self._host = f"{service}.tencentcloudapi.com"
        self._http = http or httpx.AsyncClient(base_url=f"https://{self._host}", timeout=15)
        self._clock = clock

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Returns Response without RequestId; raises TencentCloudError for an Error in the body."""
        payload = json.dumps(params, separators=(",", ":")).encode()
        timestamp = int(self._clock())
        headers = {
            "Authorization": authorization(
                secret_id=self._id,
                secret_key=self._key,
                service=self._service,
                host=self._host,
                action=action,
                payload=payload,
                timestamp=timestamp,
            ),
            "Content-Type": CONTENT_TYPE,
            "Host": self._host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": self._version,
        }
        if self._region:
            headers["X-TC-Region"] = self._region
        resp = await self._http.post("/", content=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()["Response"]
        if "Error" in body:
            err = body["Error"]
            raise TencentCloudError(err.get("Code", ""), err.get("Message", ""), body.get("RequestId"))
        body.pop("RequestId", None)
        return body
