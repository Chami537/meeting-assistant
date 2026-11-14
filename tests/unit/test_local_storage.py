"""The local object storage (STORAGE_BACKEND=local): the same interface as the cloud storage, files under
one directory, presigned links signed with LOCAL_STORAGE_SECRET (HMAC over the operation, key, expiry and
the promised sha256 for PUTs)."""

from datetime import UTC, datetime, timedelta

import pytest
from app.integrations.local_storage import LocalStorage, LocalStorageError


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "objects", base_url="http://example.test", secret="s3cret")


def now_plus(seconds=600):
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def test_put_presign_upload_and_stat(tmp_path, storage):
    source = tmp_path / "recording.m4a"
    source.write_bytes(b"audio-data")
    await storage.upload_file(source, "tasks/1/audio/upload-v1.m4a", content_type="audio/mp4")
    stat = await storage.stat("tasks/1/audio/upload-v1.m4a")
    assert stat is not None and stat.bytes == 10
    assert stat.sha256 and len(stat.sha256) == 64

    target = await storage.presign_put("tasks/1/audio/upload-v2.m4a", size=4, sha256="d" * 64,
                                       expires_at=now_plus())
    assert target.url.startswith("http://example.test/files/tasks/1/audio/upload-v2.m4a?exp=")
    assert target.headers["x-object-sha256"] == "d" * 64

    back = tmp_path / "back.m4a"
    await storage.download_file("tasks/1/audio/upload-v1.m4a", back)
    assert back.read_bytes() == b"audio-data"


def test_a_link_is_valid_only_with_its_own_signature(storage):
    expires = int(now_plus().timestamp())
    assert storage.verify("get", "tasks/1/audio/a.m4a", str(expires), "forged") is False
    assert storage.verify("put", "tasks/1/audio/a.m4a", str(expires), "forged") is False  # wrong operation
    assert storage.verify("get", "tasks/1/audio/b.m4a", str(expires), "forged") is False  # wrong key
    assert storage.verify("get", "tasks/1/audio/a.m4a", str(int(now_plus(-60).timestamp())),
                          "forged") is False  # expired


def test_a_put_link_binds_the_promised_sha256(storage):
    expires = int(now_plus().timestamp())
    storage._link("put", "tasks/1/audio/a.m4a", now_plus(), sha256="a" * 64)  # the link a Worker gets
    good = storage._sign("put", "tasks/1/audio/a.m4a", expires, "a" * 64)
    assert storage.verify("put", "tasks/1/audio/a.m4a", str(expires), good, sha256="a" * 64)
    assert storage.verify("put", "tasks/1/audio/a.m4a", str(expires), good, sha256="b" * 64) is False
    assert storage.verify("put", "tasks/1/audio/a.m4a", str(expires), "forged", sha256="a" * 64) is False


def test_keys_cannot_leave_the_root(storage):
    for key in ("../x", "a/../../x", "/etc/passwd", "a//b", ""):
        with pytest.raises(LocalStorageError):
            storage.path(key)


async def test_delete_prefix_removes_a_directory(tmp_path, storage):
    source = tmp_path / "f"
    source.write_bytes(b"x")
    await storage.upload_file(source, "tasks/1/audio/a", content_type="audio/mp4")
    await storage.upload_file(source, "tasks/1/audio/b", content_type="audio/mp4")
    await storage.delete_prefix("tasks/1/audio/")
    assert await storage.stat("tasks/1/audio/a") is None
    with pytest.raises(ValueError):
        await storage.delete_prefix("tasks/1/audio")  # a directory prefix must end in /


def test_uploaded_files_are_committed_with_their_sha256(tmp_path, storage):
    part = storage.store_stream_part("tasks/1/audio/a.m4a")
    part.write_bytes(b"payload")
    storage.commit_part("tasks/1/audio/a.m4a", "e" * 64)
    assert (storage.root / "tasks/1/audio/a.m4a").read_bytes() == b"payload"
    assert (storage.root / "tasks/1/audio/a.m4a.sha256").read_text() == "e" * 64
