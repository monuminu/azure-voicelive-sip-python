"""Application configuration modeled with Pydantic settings."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AzureVoiceLiveSettings(BaseSettings):
    """Azure Voice Live credentials and runtime parameters."""

    endpoint: str = Field(..., alias="AZURE_VOICELIVE_ENDPOINT")
    api_key: Optional[str] = Field(None, alias="AZURE_VOICELIVE_API_KEY")
    model: str = Field("gpt-4o", alias="VOICE_LIVE_MODEL")
    voice: str = Field("en-US-AvaNeural", alias="VOICE_LIVE_VOICE")
    instructions: str = Field(
        "You are a helpful AI voice assistant. Answer briefly.",
        alias="VOICE_LIVE_INSTRUCTIONS",
    )
    max_response_tokens: int = Field(200, alias="VOICE_LIVE_MAX_RESPONSE_OUTPUT_TOKENS")
    transcription_model: str = Field("AZURE_SPEECH", alias="VOICE_LIVE_TRANSCRIPTION_MODEL")
    transcription_language: str = Field("en-US", alias="VOICE_LIVE_TRANSCRIPTION_LANGUAGE")
    proactive_greeting_enabled: bool = Field(True, alias="VOICE_LIVE_PROACTIVE_GREETING_ENABLED")
    proactive_greeting: str = Field(
        "Hello! How can I assist you today?",
        alias="VOICE_LIVE_PROACTIVE_GREETING",
    )


class SipSettings(BaseSettings):
    """mjSIP-compatible configuration values."""

    local_address: str = Field("127.0.0.1", alias="SIP_LOCAL_ADDRESS")
    via_address: str = Field("127.0.0.1", alias="SIP_VIA_ADDR")
    media_address: str = Field("127.0.0.1", alias="MEDIA_ADDRESS")
    register_with_server: bool = Field(False, alias="REGISTER_WITH_SIP_SERVER")
    server: Optional[str] = Field(None, alias="SIP_SERVER")
    port: int = Field(5060, alias="SIP_PORT")
    user: Optional[str] = Field(None, alias="SIP_USER")
    auth_user: Optional[str] = Field(None, alias="AUTH_USER")
    auth_realm: Optional[str] = Field(None, alias="AUTH_REALM")
    auth_password: Optional[str] = Field(None, alias="AUTH_PASSWORD")
    display_name: str = Field("Voice Live Gateway", alias="DISPLAY_NAME")
    media_port: int = Field(10000, alias="MEDIA_PORT")
    port_count: int = Field(1000, alias="MEDIA_PORT_COUNT")


class LoggingSettings(BaseSettings):
    """Logging controls for structlog + stdlib logging."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field("INFO", alias="LOG_LEVEL")
    log_file: Optional[str] = Field("logs/gateway.log", alias="VOICE_LIVE_LOG_FILE")


class Settings(BaseSettings):
    """Top-level settings object composed of domain-specific sections."""

    model_config = SettingsConfigDict(env_file=(".env",), env_file_encoding="utf-8", extra="ignore")

    azure: AzureVoiceLiveSettings = AzureVoiceLiveSettings()
    sip: SipSettings = SipSettings()
    logging: LoggingSettings = LoggingSettings()


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """Load settings once per process to avoid repeated disk reads."""

    return Settings()
