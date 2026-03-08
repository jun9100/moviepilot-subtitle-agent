from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SearchRequest(BaseModel):
    title: str = Field(..., min_length=1)
    media_type: Literal["movie", "tv"] = "movie"
    year: int | None = Field(default=None, ge=1878, le=2100)
    season: int | None = Field(default=None, ge=1)
    episode: int | None = Field(default=None, ge=1)
    imdb_id: str | None = None
    tmdb_id: int | None = Field(default=None, ge=1)
    languages: list[str] = Field(default_factory=lambda: ["zh-cn", "zh-tw"])
    limit: int = Field(default=20, ge=1, le=200)

    @field_validator("imdb_id")
    @classmethod
    def normalize_imdb_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.startswith("tt"):
            return cleaned
        if cleaned.isdigit():
            return f"tt{cleaned}"
        return cleaned

    @field_validator("languages", mode="before")
    @classmethod
    def normalize_languages(cls, value: Any) -> list[str]:
        if value is None:
            return ["zh-cn", "zh-tw"]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return ["zh-cn", "zh-tw"]


class SubtitleSearchItem(BaseModel):
    token: str
    provider: str
    subtitle_id: str
    title: str
    language: str
    score: int
    matches: list[str] = Field(default_factory=list)
    hearing_impaired: bool | None = None
    page_link: str | None = None
    subtitle_format: str | None = None
    download_url: str | None = None


class SearchResponse(BaseModel):
    query: SearchRequest
    providers: list[str]
    total: int
    items: list[SubtitleSearchItem]


class DownloadRequest(BaseModel):
    token: str = Field(..., min_length=1)
    filename: str | None = None


class CaptchaSolveRequest(BaseModel):
    challenge_id: str = Field(..., min_length=1)
    code: str = Field(..., min_length=1, max_length=16)
    filename: str | None = None

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip()


class DownloadResponse(BaseModel):
    token: str
    provider: str
    subtitle_id: str
    filename: str
    path: str
    size: int
    sha256: str


class MoviePilotSearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title: str = Field(..., min_length=1)
    type: Literal["movie", "tv", "series", "show"] = "movie"
    year: int | None = Field(default=None, ge=1878, le=2100)
    season: int | None = Field(default=None, ge=1)
    episode: int | None = Field(default=None, ge=1)
    imdb_id: str | None = None
    tmdb_id: int | None = Field(default=None, ge=1)
    language: str | None = None
    languages: list[str] | str | None = None
    limit: int = Field(default=20, ge=1, le=200)

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        if normalized.get("imdb_id") is None and normalized.get("imdbid") is not None:
            normalized["imdb_id"] = normalized.get("imdbid")
        if normalized.get("tmdb_id") is None and normalized.get("tmdbid") is not None:
            normalized["tmdb_id"] = normalized.get("tmdbid")
        return normalized

    def to_search_request(self, default_languages: list[str]) -> SearchRequest:
        language_input: list[str] | str | None = self.languages
        if language_input is None:
            language_input = self.language

        parsed_languages: list[str]
        if isinstance(language_input, list):
            parsed_languages = [item.strip() for item in language_input if item and item.strip()]
        elif isinstance(language_input, str):
            parsed_languages = [item.strip() for item in language_input.split(",") if item.strip()]
        else:
            parsed_languages = default_languages

        media_type = "movie" if self.type == "movie" else "tv"
        return SearchRequest(
            title=self.title,
            media_type=media_type,
            year=self.year,
            season=self.season,
            episode=self.episode,
            imdb_id=self.imdb_id,
            tmdb_id=self.tmdb_id,
            languages=parsed_languages,
            limit=self.limit,
        )


class MoviePilotSubtitleItem(BaseModel):
    id: str
    provider: str
    subtitle_id: str
    name: str
    language: str
    score: int
    format: str | None = None
    hearing_impaired: bool | None = None
    page_link: str | None = None
    matches: list[str] = Field(default_factory=list)
    download_url: str


class MoviePilotEnvelope(BaseModel):
    success: bool = True
    message: str = "ok"
    data: Any = None
