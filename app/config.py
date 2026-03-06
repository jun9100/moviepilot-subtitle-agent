from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    app_name: str = "MoviePilot Subtitle Agent"
    app_version: str = "0.1.2"
    host: str = "0.0.0.0"
    port: int = 8178
    debug: bool = False

    default_languages: str = "zh-cn,zh-tw"
    default_providers: str = "assrt,subhd"
    enable_subliminal_fallback: bool = False
    subliminal_fallback_providers: str = "opensubtitlescom,podnapisi,tvsubtitles,opensubtitles"
    max_results: int = 30
    token_ttl_seconds: int = 1800
    request_timeout_seconds: int = 20
    user_agent: str = "MoviePilotSubtitleAgent/0.2"
    subtitle_output_dir: Path = Path("data/subtitles")

    addic7ed_username: str | None = None
    addic7ed_password: str | None = None

    opensubtitles_username: str | None = None
    opensubtitles_password: str | None = None

    opensubtitlescom_username: str | None = None
    opensubtitlescom_password: str | None = None
    opensubtitlescom_api_key: str | None = None

    @field_validator("debug", mode="before")
    @classmethod
    def normalize_debug_value(cls, value):  # type: ignore[no-untyped-def]
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "prod", "production"}:
                return False
        return value

    @property
    def provider_list(self) -> list[str]:
        return [item.strip() for item in self.default_providers.split(",") if item.strip()]

    @property
    def subliminal_provider_list(self) -> list[str]:
        return [item.strip() for item in self.subliminal_fallback_providers.split(",") if item.strip()]

    @property
    def language_list(self) -> list[str]:
        return [item.strip() for item in self.default_languages.split(",") if item.strip()]

    @property
    def provider_configs(self) -> dict[str, dict[str, object]]:
        timeout = self.request_timeout_seconds

        configs: dict[str, dict[str, object]] = {
            "addic7ed": {
                "username": self.addic7ed_username,
                "password": self.addic7ed_password,
                "allow_searches": True,
                "timeout": timeout,
            },
            "opensubtitles": {
                "username": self.opensubtitles_username,
                "password": self.opensubtitles_password,
                "timeout": timeout,
            },
            "opensubtitlesvip": {
                "username": self.opensubtitles_username,
                "password": self.opensubtitles_password,
                "timeout": timeout,
            },
            "opensubtitlescom": {
                "username": self.opensubtitlescom_username,
                "password": self.opensubtitlescom_password,
                "apikey": self.opensubtitlescom_api_key,
                "timeout": timeout,
            },
            "opensubtitlescomvip": {
                "username": self.opensubtitlescom_username,
                "password": self.opensubtitlescom_password,
                "apikey": self.opensubtitlescom_api_key,
                "timeout": timeout,
            },
            "podnapisi": {
                "timeout": timeout,
            },
            "tvsubtitles": {},
            "gestdown": {
                "timeout": timeout,
            },
            "napiprojekt": {
                "timeout": timeout,
            },
        }

        clean_configs: dict[str, dict[str, object]] = {}
        for provider, config in configs.items():
            clean = {key: value for key, value in config.items() if value is not None}
            if clean:
                clean_configs[provider] = clean
        return clean_configs


@lru_cache
def get_settings() -> Settings:
    return Settings()
