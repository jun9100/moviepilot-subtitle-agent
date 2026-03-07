from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    app_name: str = "MoviePilot Subtitle Agent"
    app_version: str = "0.2.9"
    host: str = "0.0.0.0"
    port: int = 8178
    debug: bool = False

    default_languages: str = "zh-cn,zh-tw"
    default_providers: str = "assrt,subhd,subhdtw"
    # Optional custom stage order, split by "|", each stage split by ",".
    # Example:
    # opensubtitlescom,opensubtitles|assrt,subhd,subhdtw|podnapisi,tvsubtitles
    provider_stage_order: str = ""
    enable_subliminal_fallback: bool = True
    # Fallback source chain:
    # 1) non-opensubtitles providers
    # 2) opensubtitles providers (last resort)
    subliminal_fallback_providers: str = "podnapisi,tvsubtitles,opensubtitlescom,opensubtitles"
    max_results: int = 30
    min_score: int = 0
    enable_parallel_search: bool = True
    search_workers: int = 6
    token_ttl_seconds: int = 1800
    request_timeout_seconds: int = 20
    user_agent: str = "MoviePilotSubtitleAgent/0.2"
    subtitle_output_dir: Path = Path("data/subtitles")
    allow_season_pack_for_episode: bool = True
    strict_media_type_filter: bool = True
    enable_content_language_validation: bool = True
    chinese_confidence_threshold: float = 0.25
    chinese_confidence_min_chars: int = 4
    subhd_captcha_cooldown_seconds: int = 1800
    subhd_cookie_string: str | None = None
    subhd_cookie_file: str | None = None

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

    @field_validator("search_workers", mode="before")
    @classmethod
    def normalize_search_workers(cls, value: Any) -> int:
        if value is None:
            return 6
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 6
        return max(1, parsed)

    @field_validator("chinese_confidence_threshold", mode="before")
    @classmethod
    def normalize_chinese_confidence_threshold(cls, value: Any) -> float:
        if value is None:
            return 0.25
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.25
        return min(1.0, max(0.0, parsed))

    @field_validator("chinese_confidence_min_chars", mode="before")
    @classmethod
    def normalize_chinese_confidence_min_chars(cls, value: Any) -> int:
        if value is None:
            return 4
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 4
        return max(1, parsed)

    @field_validator("subhd_captcha_cooldown_seconds", mode="before")
    @classmethod
    def normalize_subhd_captcha_cooldown_seconds(cls, value: Any) -> int:
        if value is None:
            return 1800
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 1800
        return max(0, parsed)

    @property
    def provider_list(self) -> list[str]:
        return [item.strip() for item in self.default_providers.split(",") if item.strip()]

    @property
    def subliminal_provider_list(self) -> list[str]:
        return [item.strip() for item in self.subliminal_fallback_providers.split(",") if item.strip()]

    @property
    def non_opensubtitles_fallback_provider_list(self) -> list[str]:
        opensubtitles_names = {"opensubtitles", "opensubtitlesvip", "opensubtitlescom", "opensubtitlescomvip"}
        return [item for item in self.subliminal_provider_list if item.lower() not in opensubtitles_names]

    @property
    def opensubtitles_fallback_provider_list(self) -> list[str]:
        opensubtitles_names = {"opensubtitles", "opensubtitlesvip", "opensubtitlescom", "opensubtitlescomvip"}
        return [item for item in self.subliminal_provider_list if item.lower() in opensubtitles_names]

    @property
    def language_list(self) -> list[str]:
        return [item.strip() for item in self.default_languages.split(",") if item.strip()]

    @property
    def provider_stage_list(self) -> list[list[str]]:
        if self.provider_stage_order.strip():
            parsed: list[list[str]] = []
            for stage in self.provider_stage_order.split("|"):
                providers = [item.strip() for item in stage.split(",") if item.strip()]
                if providers:
                    parsed.append(providers)
            if parsed:
                return parsed

        stages: list[list[str]] = []
        if self.provider_list:
            stages.append(self.provider_list)

        if self.enable_subliminal_fallback:
            non_open = self.non_opensubtitles_fallback_provider_list
            if non_open:
                stages.append(non_open)
            opensubtitles = self.opensubtitles_fallback_provider_list
            if opensubtitles:
                stages.append(opensubtitles)
        return stages

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
