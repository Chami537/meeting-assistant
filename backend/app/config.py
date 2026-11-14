from functools import lru_cache
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    database_url: str | None = None  # postgresql+asyncpg://...
    redis_url: str | None = None
    public_base_url: str | None = None

    wecom_corp_id: str | None = None
    wecom_agent_id: str | None = None
    wecom_app_secret: str | None = None
    wecom_callback_token: str | None = None
    wecom_callback_encoding_aes_key: str | None = None
    wecom_kf_open_kfid: str | None = None

    # Where recordings are kept: "cos" (Tencent Cloud COS, below) or "local" (files under LOCAL_STORAGE_DIR,
    # presigned links served by this API's /files route at LOCAL_STORAGE_URL; the offline mode uses it)
    storage_backend: Literal["cos", "local"] = "cos"
    local_storage_dir: str | None = None
    local_storage_url: str | None = None  # reachable by the recognition service; default PUBLIC_BASE_URL
    local_storage_secret: str | None = None  # signs the links

    cos_region: str | None = None
    cos_bucket: str | None = None  # "<name>-<appid>"
    cos_secret_id: str | None = None
    cos_secret_key: str | None = None

    asr_provider: Literal["tencent", "whisper", "funasr", "qwen"] = "tencent"
    asr_secret_id: str | None = None  # tencent
    asr_secret_key: str | None = None
    asr_engine_model: str = "16k_zh_en_meeting"
    # whisper: the self-hosted faster-whisper job service, e.g. http://host:18000
    asr_whisper_url: str | None = None
    asr_whisper_model: str = "faster-whisper-large-v3"
    asr_whisper_language: str = "zh"
    asr_whisper_prompt: str | None = None  # initial prompt; on Chinese meetings it made the output worse
    # funasr: our Fun-ASR-Nano service (asr_service/), the same job API plus speaker labels, e.g. http://host:8020
    asr_funasr_url: str | None = None
    # qwen: the same service (asr_service/) running Qwen3-ASR, e.g. http://host:8020
    asr_qwen_url: str | None = None
    asr_daily_audio_hours: float = 24.0  # rolling 24h cap on audio submitted for recognition, per provider
    # The AI meeting page's jobs (/meeting/parse, services/meeting_parse.py), kept in this SQLite file; unset,
    # those endpoints answer 503. Recognition goes to ASR_QWEN_URL whatever ASR_PROVIDER says.
    meeting_parse_db: str | None = None

    # "openai": an OpenAI-compatible chat completions endpoint (DeepSeek, Qwen, ...); "anthropic": Claude via
    # the official SDK (LLM_BASE_URL / LLM_API_KEY optional: the SDK then reads the ANTHROPIC_* environment)
    # "fake": the offline stand-in (integrations/fake_llm.py), no network or key; never for real minutes
    llm_api: Literal["openai", "anthropic", "fake"] = "openai"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"  # anthropic: thinking depth
    llm_base_url: str | None = None  # OpenAI-compatible endpoint, e.g. https://<vendor>/v1
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_provider: str | None = None  # recorded in meeting_minutes.model_provider
    llm_region: str | None = None
    # max_tokens. Unset: 8192 for openai, 16000 for anthropic (thinking shares it); 0 (openai): provider's
    llm_max_output_tokens: int | None = None
    # Vendor-specific request fields as a JSON object, e.g. {"thinking": {"type": "disabled"}} for DeepSeek
    llm_extra_body: dict[str, Any] | None = None

    @field_validator("llm_extra_body", "llm_max_output_tokens", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    data_encryption_keys: str | None = None  # "1:<base64>,2:<base64>"
    data_encryption_active_key: int | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
