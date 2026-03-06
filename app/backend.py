from __future__ import annotations

from typing import Any

from babelfish import Language
from subliminal.core import download_subtitles, list_subtitles
from subliminal.score import compute_score


def parse_languages(language_codes: list[str]) -> set[Language]:
    languages: set[Language] = set()
    for code in language_codes:
        normalized = code.strip().lower()
        if not normalized:
            continue

        try:
            languages.add(Language.fromietf(normalized))
            continue
        except Exception:
            pass

        try:
            languages.add(Language.fromalpha2(normalized))
            continue
        except Exception:
            pass

        try:
            languages.add(Language(normalized))
        except Exception:
            continue

    if not languages:
        languages.add(Language.fromalpha2("en"))

    return languages


def language_to_code(language: Any) -> str:
    if language is None:
        return "und"

    alpha2 = getattr(language, "alpha2", None)
    if alpha2:
        return str(alpha2)

    alpha3 = getattr(language, "alpha3", None)
    if alpha3:
        return str(alpha3)

    return str(language)


class SubliminalBackend:
    def list_subtitles(
        self,
        videos: set[Any],
        languages: set[Language],
        *,
        providers: list[str],
        provider_configs: dict[str, dict[str, object]],
    ) -> dict[Any, list[Any]]:
        return list_subtitles(
            videos,
            languages,
            providers=providers,
            provider_configs=provider_configs,
        )

    def download_subtitles(
        self,
        subtitles: list[Any],
        *,
        providers: list[str],
        provider_configs: dict[str, dict[str, object]],
    ) -> None:
        download_subtitles(
            subtitles,
            providers=providers,
            provider_configs=provider_configs,
        )

    def compute_score(self, subtitle: Any, video: Any, *, hearing_impaired: bool | None = None) -> int:
        return compute_score(subtitle, video, hearing_impaired=hearing_impaired)
