"""The connectivity check reports per service, never crashes as a whole and never prints secrets."""

import hashlib

import httpx
from app.config import Settings
from app.postprocess.providers import AsrResult, AsrState, LlmReply, ProviderError
from app.services.uploads import ObjectStat
from app.tools import smoke


class Bucket:
    def __init__(self, corrupt=False):
        self.objects = {}
        self.corrupt = corrupt

    async def upload_file(self, path, key, *, content_type):
        data = path.read_bytes()
        self.objects[key] = data

    async def stat(self, key):
        data = self.objects.get(key)
        if data is None:
            return None
        return ObjectStat(len(data), "0" * 64 if self.corrupt else hashlib.sha256(data).hexdigest())

    async def presign_get(self, key, *, expires_at):
        return f"https://cos.example/{key}"

    async def delete(self, key):
        self.objects.pop(key, None)


async def test_cos_round_trip_and_cleanup():
    bucket = Bucket()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=bucket.objects[r.url.path[1:]]))
    )
    check = await smoke.check_cos(Settings(), storage=bucket, http=http)
    assert check.ok and bucket.objects == {}
    bad = await smoke.check_cos(Settings(), storage=Bucket(corrupt=True), http=http)
    assert bad.ok is False


async def test_asr_auth_states():
    class Asr:
        def __init__(self, outcome):
            self.outcome = outcome

        async def poll(self, task_id):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    ok = await smoke.check_asr(
        Settings(), asr=Asr(AsrResult(AsrState.FAILED, error="FailedOperation.NoSuchTask"))
    )
    assert ok.ok
    denied = await smoke.check_asr(Settings(), asr=Asr(ProviderError("AuthFailure.SecretIdNotFound")))
    assert denied.ok is False and "密钥错误" in denied.detail


async def test_llm_json_mode():
    class Llm:
        def __init__(self, content):
            self.content = content

        async def complete_json(self, **kw):
            return LlmReply(self.content)

    assert (await smoke.check_llm(Settings(llm_model="m"), llm=Llm({"ok": True}))).ok
    assert (await smoke.check_llm(Settings(llm_model="m"), llm=Llm({"x": 1}))).ok is False


async def test_unconfigured_services_are_skipped_and_crashes_are_contained():
    settings = Settings(_env_file=None)
    results = await smoke.run(
        settings, [smoke.check_cos, smoke.check_asr, smoke.check_llm, smoke.check_wecom]
    )
    assert [r.ok for r in results] == [None, None, None, None]

    async def exploding(settings):
        raise RuntimeError("secret=TOPSECRET")

    exploding.__name__ = "check_boom"
    [result] = await smoke.run(settings, [exploding])
    assert result.ok is False and "TOPSECRET" not in smoke.render([result])


async def test_public_url_must_be_https():
    assert (await smoke.check_public_url(Settings(public_base_url="http://x"))).ok is False
    assert (await smoke.check_public_url(Settings(public_base_url="https://x.example"))).ok


async def test_self_hosted_asr_health():
    from test_whisper import FakeWhisper

    settings = Settings(asr_provider="whisper", asr_whisper_url="http://whisper:18000")
    fake = FakeWhisper()
    ok = await smoke.check_asr(settings, fake.client())
    assert ok.ok and "large-v3" in ok.detail and "队列空位 4" in ok.detail
    fake.ready = {"status": "loading"}
    down = await smoke.check_asr(settings, fake.client())
    assert down.ok is False and "http://whisper:18000" in down.detail
    assert fake.uploads == []  # a health check uploads nothing
    unset = await smoke.check_asr(Settings(_env_file=None, asr_provider="whisper"))
    assert unset.ok is None


async def test_fun_asr_health():
    from test_whisper import FakeWhisper

    settings = Settings(_env_file=None, asr_provider="funasr", asr_funasr_url="http://10.0.0.5:8020")
    fake = FakeWhisper()
    fake.ready["model"] = "fun-asr-nano-2512"
    ok = await smoke.check_asr(settings, fake.client(name="funasr"))
    assert ok.ok and "Fun-ASR" in ok.name and "fun-asr-nano-2512" in ok.detail
    fake.ready = {"status": "failed"}
    down = await smoke.check_asr(settings, fake.client(name="funasr"))
    assert down.ok is False and "http://10.0.0.5:8020" in down.detail
    unset = await smoke.check_asr(Settings(_env_file=None, asr_provider="funasr"))
    assert unset.ok is None and "ASR_FUNASR_URL" in unset.detail


async def test_qwen_health():
    from test_whisper import FakeWhisper

    settings = Settings(_env_file=None, asr_provider="qwen", asr_qwen_url="http://10.0.0.5:8020")
    fake = FakeWhisper()
    fake.ready["model"] = "qwen3-asr-0.6b"
    ok = await smoke.check_asr(settings, fake.client(name="qwen"))
    assert ok.ok and "Qwen" in ok.name and "qwen3-asr-0.6b" in ok.detail
    fake.ready = {"status": "upstream unavailable"}
    down = await smoke.check_asr(settings, fake.client(name="qwen"))
    assert down.ok is False and "http://10.0.0.5:8020" in down.detail
    unset = await smoke.check_asr(Settings(_env_file=None, asr_provider="qwen"))
    assert unset.ok is None and "ASR_QWEN_URL" in unset.detail


async def test_self_hosted_asr_running_another_model_than_configured_is_reported():
    """ASR_PROVIDER=qwen while the service on that port still runs Fun-ASR: a half-done deployment."""
    from test_whisper import FakeWhisper

    settings = Settings(_env_file=None, asr_provider="qwen", asr_qwen_url="http://10.0.0.5:8020")
    fake = FakeWhisper()
    fake.ready["model"] = "fun-asr-nano-2512"
    check = await smoke.check_asr(settings, fake.client(name="qwen"))
    assert check.ok is False and "fun-asr-nano-2512" in check.detail and "qwen3-asr-0.6b" in check.detail
