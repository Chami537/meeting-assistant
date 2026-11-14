"""The presigned links of the local object storage (STORAGE_BACKEND=local, integrations/local_storage.py):

    GET /files/{key}?exp=&sig=    the object (the recognition service downloads recordings from here)
    PUT /files/{key}?exp=&sig=    store an object; the x-object-sha256 header must be the signed one and
                                  match the body (a Worker's segment upload, as a presigned COS PUT)

Both answer 404 when the storage is not local, 403 for a link that is forged or expired.
"""

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from ..deps import local_storage_or_none
from ..integrations.local_storage import LocalStorage, LocalStorageError

router = APIRouter()

MAX_PUT_BYTES = 2 << 30  # an uploaded recording is at most 2 GB (upload page), a Worker segment far less


def _local() -> LocalStorage:
    try:
        storage = local_storage_or_none()
    except RuntimeError as exc:  # STORAGE_BACKEND=local without its directory or secret
        raise HTTPException(503, f"local storage not configured: {exc}") from None
    if storage is None:
        raise HTTPException(404, "Not Found")
    return storage


@router.get("/files/{object_key:path}")
async def get_file(
    object_key: str,
    storage: Annotated[LocalStorage, Depends(_local)],
    exp: str = Query(""),
    sig: str = Query(""),
) -> FileResponse:
    try:
        path = storage.path(object_key)
    except LocalStorageError:
        raise HTTPException(404, "Not Found") from None
    if not storage.verify("get", object_key, exp, sig):
        raise HTTPException(403, "link invalid or expired")
    if not path.is_file():
        raise HTTPException(404, "Not Found")
    return FileResponse(path, media_type="application/octet-stream")


@router.put("/files/{object_key:path}")
async def put_file(
    object_key: str,
    request: Request,
    storage: Annotated[LocalStorage, Depends(_local)],
    exp: str = Query(""),
    sig: str = Query(""),
    x_object_sha256: str = Header(""),
) -> Response:
    try:
        part = storage.store_stream_part(object_key)
    except LocalStorageError:
        raise HTTPException(404, "Not Found") from None
    if not x_object_sha256 or not storage.verify("put", object_key, exp, sig, x_object_sha256):
        raise HTTPException(403, "link invalid or expired")
    digest, size = hashlib.sha256(), 0
    try:
        with part.open("wb") as out:
            async for block in request.stream():
                size += len(block)
                if size > MAX_PUT_BYTES:
                    raise HTTPException(413, "object too large")
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != x_object_sha256:
            raise HTTPException(400, "body does not match x-object-sha256")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    storage.commit_part(object_key, x_object_sha256)
    return Response(status_code=200)
