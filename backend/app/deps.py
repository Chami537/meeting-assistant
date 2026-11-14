"""FastAPI dependencies. Tests override them through app.dependency_overrides."""

import dataclasses
import logging
import shutil
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from .config import Settings, get_settings
from .db import get_engine
from .integrations.claude import ClaudeLlm, build_client
from .integrations.cos import CosStorage
from .integrations.fake_llm import FakeLlm
from .integrations.llm import OpenAICompatibleLlm
from .integrations.local_storage import LocalStorage
from .integrations.tencentcloud.api import TencentCloudApi
from .integrations.tencentcloud.asr import SERVICE, VERSION, TencentAsr
from .integrations.wecom.client import WeComClient
from .integrations.wecom.crypto import CallbackCrypto
from .integrations.whisper import WhisperJobsAsr
from .postprocess.audio import BlobStore, FfmpegMerger
from .postprocess.jobs import PostProcessing
from .postprocess.media import Ffmpeg
from .postprocess.providers import AsrProvider, LlmProvider
from .security.keyring import Keyring
from .services.uploads import ObjectStorage

log = logging.getLogger(__name__)


def db_engine() -> AsyncEngine:
    return get_engine()


def clock() -> datetime:
    return datetime.now(UTC)


@lru_cache
def _keyring() -> Keyring:
    settings = get_settings()
    if not settings.data_encryption_keys or settings.data_encryption_active_key is None:
        raise RuntimeError("DATA_ENCRYPTION_KEYS / DATA_ENCRYPTION_ACTIVE_KEY are not configured")
    return Keyring.from_env(settings.data_encryption_keys, settings.data_encryption_active_key)


def keyring() -> Keyring:
    return _keyring()


@lru_cache
def _object_storage() -> CosStorage | LocalStorage:
    settings = get_settings()
    if settings.storage_backend == "local":
        if not (settings.local_storage_dir and settings.local_storage_secret):
            raise RuntimeError(
                "LOCAL_STORAGE_DIR / LOCAL_STORAGE_SECRET are not configured (STORAGE_BACKEND=local)"
            )
        return LocalStorage(
            Path(settings.local_storage_dir),
            base_url=settings.local_storage_url or settings.public_base_url or "http://127.0.0.1:8000",
            secret=settings.local_storage_secret,
        )
    required = (settings.cos_bucket, settings.cos_region, settings.cos_secret_id, settings.cos_secret_key)
    if not all(required):
        raise RuntimeError("COS_BUCKET / COS_REGION / COS_SECRET_ID / COS_SECRET_KEY are not configured")
    return CosStorage(
        bucket=settings.cos_bucket,
        region=settings.cos_region,
        secret_id=settings.cos_secret_id,
        secret_key=settings.cos_secret_key,
    )


def object_storage() -> ObjectStorage:
    return _object_storage()


def local_storage_or_none() -> LocalStorage | None:
    """The local object storage when STORAGE_BACKEND=local (its /files route serves the links), else None."""
    if get_settings().storage_backend != "local":
        return None
    storage = _object_storage()
    return storage if isinstance(storage, LocalStorage) else None


@lru_cache
def _postprocessing() -> PostProcessing:
    settings = get_settings()
    storage = object_storage()
    if not isinstance(storage, BlobStore):
        raise RuntimeError("the object storage cannot download and upload files for merging")
    return PostProcessing(
        asr=_asr(),
        llm=llm_client(),
        storage=storage,
        merger=FfmpegMerger(storage),
        asr_daily_audio_seconds=int(settings.asr_daily_audio_hours * 3600),
        inbound=_wecom_client() if settings.wecom_corp_id and settings.wecom_app_secret else None,
        keyring=_keyring(),
    )


@lru_cache
def _ffmpeg() -> Ffmpeg:
    # lru_cache does not keep exceptions: until ffmpeg is installed every call looks for it again.
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("ffmpeg/ffprobe not installed: uploaded recordings cannot be processed")
    return Ffmpeg()


def llm_client(settings: Settings | None = None) -> LlmProvider:
    """The minutes model named by LLM_API (also used by the smoke check, so both send the same request)."""
    settings = settings or get_settings()
    if settings.llm_api == "fake":  # offline mode: extractive stand-in, never for real minutes
        return FakeLlm()
    if settings.llm_api == "anthropic":
        if not (settings.llm_model and settings.llm_provider):
            raise RuntimeError("LLM_MODEL / LLM_PROVIDER are not configured (LLM_API=anthropic)")
        if bool(settings.llm_api_key) != bool(settings.llm_base_url):
            # One without the other sends an ANTHROPIC_* credential to another vendor's URL, or another
            # vendor's key (a DeepSeek LLM_API_KEY left over) to Anthropic.
            raise RuntimeError("LLM_API=anthropic: set LLM_BASE_URL and LLM_API_KEY together, or neither")
        return ClaudeLlm(
            model=settings.llm_model,
            provider=settings.llm_provider,
            region=settings.llm_region or "",
            effort=settings.llm_effort,
            max_output_tokens=settings.llm_max_output_tokens or 16000,
            client=build_client(api_key=settings.llm_api_key or None, base_url=settings.llm_base_url or None),
        )
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model and settings.llm_provider):
        raise RuntimeError("LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_PROVIDER are not configured")
    return OpenAICompatibleLlm(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        provider=settings.llm_provider,
        region=settings.llm_region or "",
        max_output_tokens=8192 if settings.llm_max_output_tokens is None else settings.llm_max_output_tokens,
        extra_body=settings.llm_extra_body,
    )


def _asr() -> AsrProvider:
    settings = get_settings()
    if settings.asr_provider == "whisper":
        if not settings.asr_whisper_url:
            raise RuntimeError("ASR_WHISPER_URL is not configured (ASR_PROVIDER=whisper)")
        return WhisperJobsAsr(
            settings.asr_whisper_url,
            model=settings.asr_whisper_model,
            language=settings.asr_whisper_language or None,
            prompt=settings.asr_whisper_prompt or None,
        )
    if settings.asr_provider == "qwen":
        if not settings.asr_qwen_url:
            raise RuntimeError("ASR_QWEN_URL is not configured (ASR_PROVIDER=qwen)")
        return WhisperJobsAsr(
            settings.asr_qwen_url,
            model="qwen3-asr-0.6b",
            language="zh",
            name="qwen",
            context=lambda title: title,  # the service adds the glossary (asr_service/glossary.py)
        )
    if settings.asr_provider == "funasr":
        if not settings.asr_funasr_url:
            raise RuntimeError("ASR_FUNASR_URL is not configured (ASR_PROVIDER=funasr)")
        return WhisperJobsAsr(
            settings.asr_funasr_url, model="fun-asr-nano-2512", language="zh", name="funasr"
        )
    if not (settings.asr_secret_id and settings.asr_secret_key):
        raise RuntimeError("ASR_SECRET_ID / ASR_SECRET_KEY are not configured")
    cloud = TencentCloudApi(settings.asr_secret_id, settings.asr_secret_key, service=SERVICE, version=VERSION)
    return TencentAsr(cloud, engine=settings.asr_engine_model)


def _media_tool() -> Ffmpeg | None:
    """Without ffmpeg/ffprobe uploaded recordings wait (the ingest job reports it) instead of failing one by
    one; installing ffmpeg takes effect on the next pass, without a restart."""
    try:
        return _ffmpeg()
    except RuntimeError:
        return None


def postprocessing() -> PostProcessing:
    return dataclasses.replace(_postprocessing(), media=_media_tool())


@lru_cache
def _wecom_crypto() -> CallbackCrypto:
    settings = get_settings()
    if not (
        settings.wecom_callback_token and settings.wecom_callback_encoding_aes_key and settings.wecom_corp_id
    ):
        raise RuntimeError(
            "WECOM_CALLBACK_TOKEN / WECOM_CALLBACK_ENCODING_AES_KEY / WECOM_CORP_ID are not configured"
        )
    return CallbackCrypto(
        token=settings.wecom_callback_token,
        encoding_aes_key=settings.wecom_callback_encoding_aes_key,
        receive_id=settings.wecom_corp_id,
    )


def wecom_crypto() -> CallbackCrypto:
    return _wecom_crypto()


@lru_cache
def _wecom_client() -> WeComClient:
    settings = get_settings()
    if not (settings.wecom_corp_id and settings.wecom_app_secret):
        raise RuntimeError("WECOM_CORP_ID / WECOM_APP_SECRET are not configured")
    return WeComClient(settings.wecom_corp_id, settings.wecom_app_secret)


def wecom_client() -> WeComClient:
    return _wecom_client()
