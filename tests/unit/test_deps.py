"""Dependency wiring that has behavior of its own."""

import dataclasses

import pytest
from app import deps
from app.postprocess.jobs import PostProcessing
from app.postprocess.media import Ffmpeg


@pytest.fixture
def fresh_ffmpeg_cache():
    deps._ffmpeg.cache_clear()
    yield
    deps._ffmpeg.cache_clear()


def test_ffmpeg_installed_later_is_picked_up_without_a_restart(monkeypatch, fresh_ffmpeg_cache):
    base = PostProcessing(asr=None, llm=None, storage=None, merger=None, asr_daily_audio_seconds=1)
    monkeypatch.setattr(deps, "_postprocessing", lambda: base)
    installed: dict[str, str] = {}
    monkeypatch.setattr(deps.shutil, "which", installed.get)

    assert deps.postprocessing().media is None
    installed.update(ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
    pipeline = deps.postprocessing()
    assert isinstance(pipeline.media, Ffmpeg)
    assert dataclasses.replace(pipeline, media=None) == base


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [("", None), ("  ", None), ('{"thinking": {"type": "disabled"}}', {"thinking": {"type": "disabled"}})],
)
def test_llm_extra_body_setting(monkeypatch, raw, parsed):
    from app.config import Settings

    monkeypatch.setenv("LLM_EXTRA_BODY", raw)
    assert Settings(_env_file=None).llm_extra_body == parsed


def _llm_settings(**kw):
    from app.config import Settings

    return Settings(_env_file=None, llm_model="m", llm_provider="p", **kw)


@pytest.fixture
def no_anthropic_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)


def test_llm_client_builds_the_configured_kind(no_anthropic_env):
    from app.integrations.claude import ClaudeLlm
    from app.integrations.llm import OpenAICompatibleLlm

    openai = deps.llm_client(_llm_settings(llm_base_url="https://x.example/v1", llm_api_key="k"))
    assert isinstance(openai, OpenAICompatibleLlm) and openai._max_output_tokens == 8192
    claude = deps.llm_client(
        _llm_settings(
            llm_api="anthropic", llm_base_url="https://gw.example", llm_api_key="k", llm_effort="low"
        )
    )
    assert isinstance(claude, ClaudeLlm) and (claude._effort, claude._max_tokens) == ("low", 16000)
    assert claude._client.max_retries == 0  # retries belong to the job


def test_llm_client_refuses_anthropic_without_credentials(no_anthropic_env):
    with pytest.raises(RuntimeError, match="credentials are not configured"):
        deps.llm_client(_llm_settings(llm_api="anthropic"))


@pytest.mark.parametrize(
    "half",
    [{"llm_base_url": "https://api.deepseek.com"}, {"llm_api_key": "sk-deepseek-left-over"}],
)
def test_llm_client_never_mixes_vendors_credentials(no_anthropic_env, monkeypatch, half):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-for-the-gateway")
    with pytest.raises(RuntimeError, match="together, or neither"):
        deps.llm_client(_llm_settings(llm_api="anthropic", **half))


def test_llm_effort_is_checked_when_loading_settings():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _llm_settings(llm_effort="meduim")


def test_fun_asr_provider_is_the_job_client_under_its_own_name(monkeypatch):
    from app.config import Settings
    from app.integrations.whisper import WhisperJobsAsr

    funasr = Settings(_env_file=None, asr_provider="funasr", asr_funasr_url="http://10.0.0.5:8020")
    monkeypatch.setattr(deps, "get_settings", lambda: funasr)
    asr = deps._asr()
    assert isinstance(asr, WhisperJobsAsr) and (asr.name, asr._model) == ("funasr", "fun-asr-nano-2512")
    assert asr.context is None  # only the Qwen engine uses the context
    monkeypatch.setattr(deps, "get_settings", lambda: Settings(_env_file=None, asr_provider="funasr"))
    with pytest.raises(RuntimeError, match="ASR_FUNASR_URL"):
        deps._asr()


def test_qwen_provider_is_the_job_client_under_its_own_name(monkeypatch):
    from app.config import Settings
    from app.integrations.whisper import WhisperJobsAsr

    qwen = Settings(_env_file=None, asr_provider="qwen", asr_qwen_url="http://10.0.0.5:8020")
    monkeypatch.setattr(deps, "get_settings", lambda: qwen)
    asr = deps._asr()
    assert isinstance(asr, WhisperJobsAsr) and (asr.name, asr._model) == ("qwen", "qwen3-asr-0.6b")
    assert asr.context("03-晨川科技") == "03-晨川科技"  # the title; the service adds the glossary
    monkeypatch.setattr(deps, "get_settings", lambda: Settings(_env_file=None, asr_provider="qwen"))
    with pytest.raises(RuntimeError, match="ASR_QWEN_URL"):
        deps._asr()
