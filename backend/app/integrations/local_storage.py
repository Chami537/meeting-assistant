"""Object storage as files under one directory (STORAGE_BACKEND=local), for the offline mode and for anyone
running the service without a cloud bucket. It speaks the same interface as CosStorage (ObjectStorage for
the Worker uploads and purges, BlobStore for the jobs' downloads and uploads), so nothing else changes.

Presigned links point at this service's own /files route (api/files.py): an HMAC of the operation, the key
and the expiry, checked there. The recognition service downloads a recording from such a link exactly as it
downloads one from COS, so LOCAL_STORAGE_URL must be an address it can reach (in docker compose the API's
service name). A PUT link carries the sha256 the uploader promised, as the COS one does; the PUT is refused
when the body does not match it.

Each object is a file; its sha256 sits next to it in "<file>.sha256" (COS keeps it as object metadata).
"""

import asyncio
import hashlib
import hmac
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from ..services.uploads import ObjectStat, UploadTarget

SHA256_HEADER = "x-object-sha256"
_IO_BLOCK = 1 << 20


class LocalStorageError(Exception):
    pass


class LocalStorage:
    def __init__(self, root: Path, *, base_url: str, secret: str, clock: Callable[[], float] = time.time):
        if not secret:
            raise ValueError("a signing secret is required")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self._secret = secret.encode()
        self._clock = clock

    # ------------------------------------------------------------ keys and signatures

    def path(self, object_key: str) -> Path:
        """`object_key`'s file; a key that would leave the root (.., absolute, empty parts) is refused."""
        parts = object_key.split("/")
        if object_key.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise LocalStorageError("invalid object key")
        if parts[-1].endswith(".sha256"):
            raise LocalStorageError("invalid object key")
        return self.root.joinpath(*parts)

    def _sign(self, op: str, object_key: str, expires: int, sha256: str = "") -> str:
        msg = f"{op}\n{object_key}\n{expires}\n{sha256}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def verify(self, op: str, object_key: str, expires: str, signature: str, sha256: str = "") -> bool:
        if not expires.isdigit() or int(expires) < self._clock():
            return False
        return hmac.compare_digest(self._sign(op, object_key, int(expires), sha256), signature)

    def _link(self, op: str, object_key: str, expires_at: datetime, sha256: str = "") -> str:
        expires = int(expires_at.timestamp())
        query = {"exp": str(expires), "sig": self._sign(op, object_key, expires, sha256)}
        return f"{self.base_url}/files/{quote(object_key, safe='/-_.~')}?{urlencode(query)}"

    # ------------------------------------------------------------ ObjectStorage

    async def presign_put(
        self, object_key: str, *, size: int, sha256: str, expires_at: datetime
    ) -> UploadTarget:
        self.path(object_key)  # refuse a bad key now, not at upload time
        return UploadTarget(
            object_key=object_key,
            url=self._link("put", object_key, expires_at, sha256),
            expires_at=expires_at,
            headers={SHA256_HEADER: sha256},
        )

    async def presign_get(self, object_key: str, *, expires_at: datetime) -> str:
        self.path(object_key)
        return self._link("get", object_key, expires_at)

    async def stat(self, object_key: str) -> ObjectStat | None:
        file = self.path(object_key)
        if not file.is_file():
            return None
        sidecar = file.with_name(file.name + ".sha256")
        sha = sidecar.read_text().strip() if sidecar.is_file() else None
        return ObjectStat(bytes=file.stat().st_size, sha256=sha or None)

    async def delete(self, object_key: str) -> None:
        file = self.path(object_key)
        file.unlink(missing_ok=True)
        file.with_name(file.name + ".sha256").unlink(missing_ok=True)

    async def delete_prefix(self, prefix: str) -> None:
        if not prefix.endswith("/") or prefix.startswith("/"):
            raise ValueError("prefix must be a directory like tasks/<id>/audio/")
        directory = self.path(prefix.rstrip("/"))
        if directory.is_dir():
            await asyncio.to_thread(shutil.rmtree, directory)

    # ------------------------------------------------------------ BlobStore

    async def download_file(self, object_key: str, path: Path) -> None:
        file = self.path(object_key)
        if not file.is_file():
            raise LocalStorageError("no such object")
        await asyncio.to_thread(shutil.copyfile, file, path)

    async def upload_file(self, path: Path, object_key: str, *, content_type: str) -> None:
        await asyncio.to_thread(self._store_file, Path(path), object_key)

    def _store_file(self, source: Path, object_key: str) -> None:
        file = self.path(object_key)
        file.parent.mkdir(parents=True, exist_ok=True)
        part = file.with_name(file.name + ".part")
        digest = hashlib.sha256()
        with source.open("rb") as src, part.open("wb") as out:
            while block := src.read(_IO_BLOCK):
                digest.update(block)
                out.write(block)
        part.replace(file)
        file.with_name(file.name + ".sha256").write_text(digest.hexdigest())

    def store_stream_part(self, object_key: str) -> Path:
        """Where a streamed PUT body is written before it is checked (api/files.py)."""
        file = self.path(object_key)
        file.parent.mkdir(parents=True, exist_ok=True)
        return file.with_name(file.name + ".part")

    def commit_part(self, object_key: str, sha256: str) -> None:
        file = self.path(object_key)
        file.with_name(file.name + ".part").replace(file)
        file.with_name(file.name + ".sha256").write_text(sha256)
