from __future__ import annotations

from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_localhost(hostname: str) -> bool:
    normalized = (hostname or "").strip().strip("[]").lower()
    return normalized in {"localhost", "127.0.0.1", "::1"}


class AudioSettings(BaseSettings):
    audio_ws_url: str
    audio_ws_password: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("audio_ws_url")
    @classmethod
    def validate_audio_ws_url(cls, value: str) -> str:
        url = (value or "").strip()
        if not url:
            raise ValueError(
                "AUDIO_WS_URL is not defined. "
                "Please create a .env file with AUDIO_WS_URL=wss://your-server"
            )
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(
                f"Unsupported AUDIO_WS_URL scheme '{parsed.scheme}'. "
                "Use ws:// or wss://."
            )
        if parsed.scheme == "ws" and not _is_localhost(parsed.hostname or ""):
            raise ValueError(
                "Insecure ws:// is only allowed for localhost. "
                "Use wss:// for remote hosts."
            )
        return url

    @field_validator("audio_ws_password", mode="before")
    @classmethod
    def normalize_audio_ws_password(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


@lru_cache(maxsize=1)
def get_settings() -> AudioSettings:
    try:
        return AudioSettings()
    except ValidationError as exc:
        message = str(exc)
        if "audio_ws_url" in message.lower() and "field required" in message.lower():
            raise RuntimeError(
                "AUDIO_WS_URL is not defined. "
                "Please create a .env file with AUDIO_WS_URL=wss://your-server"
            ) from exc
        raise RuntimeError(f"Invalid audio configuration: {exc}") from exc


settings = get_settings()
AUDIO_WS_URL = settings.audio_ws_url
AUDIO_WS_PASSWORD = settings.audio_ws_password
