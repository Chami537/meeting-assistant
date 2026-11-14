"""Operator CLI: Worker registration and token lifecycle."""

import io
import os
import re

import httpx
import pytest
from app import cli
from app.deps import db_engine, keyring
from app.main import app
from app.security.keyring import Keyring

RING = Keyring({1: os.urandom(32)}, 1)


@pytest.fixture
async def api(engine, clean):
    app.dependency_overrides.update({db_engine: lambda: engine, keyring: lambda: RING})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def call(engine, *argv):
    out = io.StringIO()
    code = await cli.run(list(argv), engine, out=out)
    return code, out.getvalue()


async def lease_status(api, token):
    r = await api.post("/v1/workers/lease", headers={"Authorization": f"Bearer {token}"})
    return r.status_code


async def test_worker_lifecycle(engine, api):
    code, out = await call(engine, "worker", "add", "win-01")
    assert code == 0
    token = out.splitlines()[-1]
    assert len(token) == 43
    assert await lease_status(api, token) == 204  # authenticated, nothing to do

    assert (await call(engine, "worker", "add", "win-01"))[0] == 1  # names are unique

    code, out = await call(engine, "worker", "rotate", "win-01")
    new_token = out.splitlines()[-1]
    assert await lease_status(api, token) == 401
    assert await lease_status(api, new_token) == 204

    await call(engine, "worker", "disable", "win-01")
    assert await lease_status(api, new_token) == 401
    await call(engine, "worker", "enable", "win-01")
    assert await lease_status(api, new_token) == 204

    code, out = await call(engine, "worker", "list")
    assert re.match(r"win-01\twindows\tONLINE\t\d{4}-", out)  # lease calls mark it online

    await call(engine, "worker", "revoke", "win-01")
    assert await lease_status(api, new_token) == 401
    assert (await call(engine, "worker", "enable", "win-01"))[0] == 1  # revoked is final
    assert (await call(engine, "worker", "rotate", "win-01"))[0] == 1
    assert "REVOKED" in (await call(engine, "worker", "list"))[1]


async def test_unknown_worker(engine, clean):
    assert (await call(engine, "worker", "disable", "nope"))[0] == 1


async def test_new_key():
    out = io.StringIO()
    assert await cli.run(["keys", "new"], out=out) == 0
    assert len(out.getvalue().strip()) == 44
