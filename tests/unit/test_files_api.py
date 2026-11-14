"""The /files routes behind the local object storage's presigned links: only a link its secret signed works,
a PUT must carry the sha256 the link promised and a body that matches it, and the routes do not exist when
the storage is not local."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.api import files as files_api
from app.integrations.local_storage import LocalStorage
from app.main import app
from fastapi.testclient import TestClient


def make_storage(tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path / "objects", base_url="http://testserver", secret="s3cret")
    monkeypatch.setattr(files_api, "local_storage_or_none", lambda: storage)
    return storage


def now_plus(seconds=600):
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def _store(storage, key, body):
    tmp = Path("/tmp") / key.replace("/", "_")
    tmp.write_bytes(body)
    try:
        await storage.upload_file(tmp, key, content_type="audio/mp4")
    finally:
        tmp.unlink(missing_ok=True)


def test_a_valid_get_link_returns_the_object(tmp_path, monkeypatch):
    storage = make_storage(tmp_path, monkeypatch)
    asyncio.run(_store(storage, "tasks/1/audio/a.m4a", b"audio"))
    url = asyncio.run(storage.presign_get("tasks/1/audio/a.m4a", expires_at=now_plus()))
    with TestClient(app) as client:
        resp = client.get(url)
        assert resp.status_code == 200 and resp.content == b"audio"
        tampered = url.rsplit("sig=", 1)[0] + "sig=forged"
        assert client.get(tampered).status_code == 403
        assert client.get("/files/tasks/1/audio/missing.m4a").status_code == 403  # a key that was never signed
        unsigned = asyncio.run(storage.presign_get("tasks/1/audio/missing.m4a", expires_at=now_plus()))
        assert client.get(unsigned).status_code == 404  # signed, but the object is not there


def test_a_put_link_stores_the_object_and_checks_the_body(tmp_path, monkeypatch):
    storage = make_storage(tmp_path, monkeypatch)
    body = b"recording bytes"
    digest = hashlib.sha256(body).hexdigest()
    key = "tasks/1/audio/u.m4a"
    link = asyncio.run(storage.presign_put(key, size=len(body), sha256=digest, expires_at=now_plus())).url
    with TestClient(app) as client:
        assert client.put(link, content=body, headers={"x-object-sha256": digest}).status_code == 200
        assert client.put(link, content=b"other", headers={"x-object-sha256": digest}).status_code == 400
        forged = link.rsplit("sig=", 1)[0] + "sig=forged"
        assert client.put(forged, content=body, headers={"x-object-sha256": digest}).status_code == 403
        good = asyncio.run(storage.presign_get(key, expires_at=now_plus()))
        assert client.get(good).content == body


def test_an_expired_link_is_refused(tmp_path, monkeypatch):
    storage = make_storage(tmp_path, monkeypatch)
    asyncio.run(_store(storage, "tasks/1/audio/a.m4a", b"audio"))
    old = asyncio.run(storage.presign_get("tasks/1/audio/a.m4a", expires_at=now_plus(-60)))
    with TestClient(app) as client:
        assert client.get(old).status_code == 403


def test_the_routes_404_when_the_storage_is_not_local(monkeypatch):
    monkeypatch.setattr(files_api, "local_storage_or_none", lambda: None)
    with TestClient(app) as client:
        assert client.get("/files/tasks/1/audio/a.m4a").status_code == 404
        assert client.put("/files/tasks/1/audio/a.m4a", content=b"x").status_code == 404


def test_the_routes_503_on_a_half_configured_local_storage(monkeypatch):
    def broken():
        raise RuntimeError("LOCAL_STORAGE_DIR / LOCAL_STORAGE_SECRET are not configured")

    monkeypatch.setattr(files_api, "local_storage_or_none", broken)
    with TestClient(app) as client:
        assert client.get("/files/tasks/1/audio/a.m4a").status_code == 503
