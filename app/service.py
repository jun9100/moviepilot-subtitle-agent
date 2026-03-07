from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote
from uuid import uuid4

from subliminal.video import Episode, Movie

from .backend import SubliminalBackend, language_to_code, parse_languages
from .chinese_provider import ChineseSubtitleProvider, DirectSubtitleCandidate, DownloadedSubtitle
from .config import Settings
from .errors import SubtitleDownloadError, SubtitleNotFoundError, SubtitleSearchError
from .models import DownloadResponse, SearchRequest, SearchResponse, SubtitleSearchItem


@dataclass
class CachedSubtitle:
    kind: str
    payload: Any
    query: SearchRequest
    created_at: datetime
    stage_index: int | None = None


@dataclass
class InMemorySubtitle:
    token: str
    subtitle_id: str
    provider: str
    filename: str
    subtitle_format: str
    content: bytes


class SubtitleService:
    _DIRECT_PROVIDER_NAMES = {"assrt", "subhd", "subhdtw"}

    def __init__(
        self,
        *,
        settings: Settings,
        backend: SubliminalBackend | None = None,
        chinese_provider: ChineseSubtitleProvider | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._backend = backend or SubliminalBackend()
        self._chinese_provider = chinese_provider or ChineseSubtitleProvider(
            timeout_seconds=settings.request_timeout_seconds,
            user_agent=settings.user_agent,
            allow_season_pack_for_episode=settings.allow_season_pack_for_episode,
        )
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._cache: dict[str, CachedSubtitle] = {}

    def search(self, query: SearchRequest) -> SearchResponse:
        self._cleanup_cache()

        results: list[SubtitleSearchItem] = []
        last_error: Exception | None = None

        for stage_index, stage_providers in enumerate(self._settings.provider_stage_list):
            stage_results, stage_error = self._search_stage_items(
                query=query,
                stage_index=stage_index,
                stage_providers=stage_providers,
            )
            if stage_error is not None:
                last_error = stage_error

            stage_results = [item for item in stage_results if item.score >= self._settings.min_score]
            if stage_results:
                stage_results.sort(key=lambda item: item.score, reverse=True)
                results = stage_results
                break

        results.sort(key=lambda item: item.score, reverse=True)
        limit = min(query.limit, self._settings.max_results)

        active_providers = sorted({item.provider for item in results})
        if not active_providers:
            configured = self._settings.provider_list
            if configured:
                active_providers = configured

        if not results and last_error and not active_providers:
            raise SubtitleSearchError(f"subtitle search failed: {last_error}") from last_error

        return SearchResponse(
            query=query,
            providers=active_providers,
            total=len(results),
            items=results[:limit],
        )

    def _search_stage_items(
        self,
        *,
        query: SearchRequest,
        stage_index: int,
        stage_providers: list[str],
    ) -> tuple[list[SubtitleSearchItem], Exception | None]:
        direct_providers = [item for item in stage_providers if item.lower() in self._DIRECT_PROVIDER_NAMES]
        subliminal_providers = [item for item in stage_providers if item.lower() not in self._DIRECT_PROVIDER_NAMES]

        tasks: list[tuple[str, Callable[[], list[SubtitleSearchItem]]]] = []
        if self._settings.enable_parallel_search:
            for provider in direct_providers:
                tasks.append(
                    (
                        f"direct:{provider}",
                        lambda provider=provider: self._search_with_direct_providers(
                            query=query,
                            providers=[provider],
                            stage_index=stage_index,
                        ),
                    )
                )
            for provider in subliminal_providers:
                tasks.append(
                    (
                        f"subliminal:{provider}",
                        lambda provider=provider: self._search_with_subliminal_providers(
                            query=query,
                            providers=[provider],
                            stage_index=stage_index,
                        ),
                    )
                )
        else:
            if direct_providers:
                tasks.append(
                    (
                        "direct",
                        lambda: self._search_with_direct_providers(
                            query=query,
                            providers=direct_providers,
                            stage_index=stage_index,
                        ),
                    )
                )
            if subliminal_providers:
                tasks.append(
                    (
                        "subliminal",
                        lambda: self._search_with_subliminal_providers(
                            query=query,
                            providers=subliminal_providers,
                            stage_index=stage_index,
                        ),
                    )
                )

        if not tasks:
            return [], None

        stage_results: list[SubtitleSearchItem] = []
        stage_error: Exception | None = None

        if (not self._settings.enable_parallel_search) or len(tasks) == 1:
            for _label, task in tasks:
                try:
                    stage_results.extend(task())
                except Exception as exc:
                    stage_error = exc
        else:
            max_workers = max(1, min(self._settings.search_workers, len(tasks)))
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="subtitle-stage") as pool:
                futures = {pool.submit(task): label for label, task in tasks}
                for future in as_completed(futures):
                    try:
                        stage_results.extend(future.result())
                    except Exception as exc:
                        stage_error = exc

        stage_results = self._dedupe_search_items(stage_results)
        return stage_results, stage_error

    def _search_with_direct_providers(
        self,
        *,
        query: SearchRequest,
        providers: list[str],
        stage_index: int | None = None,
    ) -> list[SubtitleSearchItem]:
        try:
            direct_candidates = self._chinese_provider.search(query, providers=providers)
        except Exception as exc:
            raise SubtitleSearchError(f"chinese subtitle search failed: {exc}") from exc

        items: list[SubtitleSearchItem] = []
        for candidate in direct_candidates:
            token = uuid4().hex
            with self._lock:
                self._cache[token] = CachedSubtitle(
                    kind="direct",
                    payload=candidate,
                    query=query,
                    created_at=self._now_fn(),
                    stage_index=stage_index,
                )

            items.append(
                SubtitleSearchItem(
                    token=token,
                    provider=candidate.provider,
                    subtitle_id=candidate.subtitle_id,
                    title=candidate.release_name or candidate.title,
                    language=candidate.language,
                    score=candidate.score,
                    matches=candidate.matches,
                    hearing_impaired=None,
                    page_link=candidate.page_link,
                    subtitle_format=candidate.subtitle_format,
                    download_url=f"/api/v1/subtitles/fetch/{token}",
                )
            )
        return items

    def download_to_disk(self, token: str, filename: str | None = None) -> DownloadResponse:
        payload = self.fetch_to_memory(token, filename=filename)

        output_dir = self._settings.subtitle_output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        destination = output_dir / payload.filename
        destination.write_bytes(payload.content)

        digest = hashlib.sha256(payload.content).hexdigest()

        return DownloadResponse(
            token=payload.token,
            provider=payload.provider,
            subtitle_id=payload.subtitle_id,
            filename=payload.filename,
            path=str(destination.resolve()),
            size=len(payload.content),
            sha256=digest,
        )

    def fetch_to_memory(self, token: str, filename: str | None = None) -> InMemorySubtitle:
        entry = self._get_cached_subtitle(token)

        if entry.kind == "direct":
            return self._fetch_direct_subtitle(token, entry, filename=filename)

        if entry.kind == "subliminal":
            return self._fetch_subliminal_subtitle(token, entry, filename=filename)

        raise SubtitleNotFoundError("unknown subtitle cache entry")

    def _fetch_direct_subtitle(
        self,
        token: str,
        entry: CachedSubtitle,
        *,
        filename: str | None,
    ) -> InMemorySubtitle:
        candidate = entry.payload
        if not isinstance(candidate, DirectSubtitleCandidate):
            raise SubtitleDownloadError("invalid direct subtitle payload")

        query = entry.query
        requires_chinese = self._requires_chinese_subtitle(query.languages)

        candidates_to_try = [candidate]
        if requires_chinese:
            candidates_to_try.extend(
                self._direct_fallback_candidates(
                    query=query,
                    exclude={self._candidate_key(candidate)},
                )
            )

        max_attempts = 8
        attempts = 0
        last_error: Exception | None = None
        fallback_checked = False

        for current_candidate in candidates_to_try:
            if attempts >= max_attempts:
                break
            attempts += 1

            try:
                downloaded = self._chinese_provider.download(current_candidate, query=query)
            except Exception as exc:
                last_error = exc
                continue

            if not downloaded.content:
                last_error = SubtitleDownloadError("subtitle content is empty")
                continue

            if requires_chinese and not self._content_has_chinese_text(downloaded.content):
                last_error = SubtitleDownloadError("downloaded subtitle content does not contain Chinese text")
                continue

            return self._build_in_memory_direct(
                token=token,
                query=query,
                candidate=current_candidate,
                downloaded=downloaded,
                filename=filename,
            )

        if self._settings.enable_subliminal_fallback:
            fallback_checked = True
            try:
                fallback_subtitle = self._fetch_with_subliminal_fallback(
                    query=query,
                    filename=filename,
                    requires_chinese=requires_chinese,
                    response_token=token,
                )
            except Exception as exc:
                if last_error:
                    raise SubtitleDownloadError(
                        f"failed to get verified Chinese subtitle from direct candidates: {last_error}; "
                        f"subliminal fallback failed: {exc}"
                    ) from exc
                raise SubtitleDownloadError(f"subliminal fallback failed: {exc}") from exc

            if fallback_subtitle is not None:
                return fallback_subtitle

        if last_error:
            if fallback_checked:
                providers = ",".join(self._settings.subliminal_provider_list) or "none"
                raise SubtitleDownloadError(
                    "failed to get verified Chinese subtitle from direct candidates: "
                    f"{last_error}; fallback providers attempted ({providers}) "
                    "but no verified Chinese subtitle found"
                ) from last_error
            raise SubtitleDownloadError(
                f"failed to get verified Chinese subtitle from direct candidates: {last_error}"
            ) from last_error

        raise SubtitleDownloadError("failed to get verified Chinese subtitle from direct candidates")

    def _fetch_with_subliminal_fallback(
        self,
        *,
        query: SearchRequest,
        filename: str | None,
        requires_chinese: bool,
        response_token: str,
    ) -> InMemorySubtitle | None:
        provider_groups: list[tuple[int, list[str]]] = []
        for stage_index, stage in enumerate(self._settings.provider_stage_list):
            subliminal_stage = [item for item in stage if item.lower() not in self._DIRECT_PROVIDER_NAMES]
            if subliminal_stage:
                provider_groups.append((stage_index, subliminal_stage))
        download_error: Exception | None = None
        search_error: Exception | None = None

        for stage_index, providers in provider_groups:
            if not providers:
                continue

            items, stage_error = self._search_stage_items(
                query=query,
                stage_index=stage_index,
                stage_providers=providers,
            )
            if stage_error is not None:
                search_error = stage_error
                if not items:
                    continue

            items = [item for item in items if item.score >= self._settings.min_score]
            items.sort(key=lambda item: item.score, reverse=True)
            for item in items:
                try:
                    fallback_entry = self._get_cached_subtitle(item.token)
                    fetched = self._fetch_entry_once(
                        response_token=response_token,
                        entry=fallback_entry,
                        filename=filename,
                        requires_chinese=requires_chinese,
                    )
                except Exception as exc:
                    download_error = exc
                    continue

                return fetched

        if download_error is not None:
            raise SubtitleDownloadError(str(download_error)) from download_error
        if search_error is not None:
            raise SubtitleDownloadError(str(search_error)) from search_error
        return None

    def _fetch_subliminal_subtitle(
        self,
        token: str,
        entry: CachedSubtitle,
        *,
        filename: str | None,
    ) -> InMemorySubtitle:
        query = entry.query
        requires_chinese = self._requires_chinese_subtitle(query.languages)

        try:
            return self._fetch_entry_once(
                response_token=token,
                entry=entry,
                filename=filename,
                requires_chinese=requires_chinese,
            )
        except Exception as exc:
            primary_error = exc

        start_stage_index = self._resolve_stage_index(entry)
        exclude_keys = set()
        failed_key = self._entry_key(entry)
        if failed_key is not None:
            exclude_keys.add(failed_key)

        try:
            fallback = self._fetch_with_stage_failover(
                query=query,
                filename=filename,
                requires_chinese=requires_chinese,
                response_token=token,
                start_stage_index=start_stage_index,
                exclude_keys=exclude_keys,
            )
        except Exception as exc:
            raise SubtitleDownloadError(
                f"subtitle download failed: {primary_error}; stage failover failed: {exc}"
            ) from exc

        if fallback is not None:
            return fallback

        raise SubtitleDownloadError(
            f"subtitle download failed: {primary_error}; stage failover exhausted"
        ) from primary_error

    def _fetch_with_stage_failover(
        self,
        *,
        query: SearchRequest,
        filename: str | None,
        requires_chinese: bool,
        response_token: str,
        start_stage_index: int,
        exclude_keys: set[tuple[str, str]] | None = None,
    ) -> InMemorySubtitle | None:
        stages = self._settings.provider_stage_list
        if not stages:
            return None

        seen_keys = set(exclude_keys or set())
        last_download_error: Exception | None = None
        last_search_error: Exception | None = None

        for stage_index in range(start_stage_index, len(stages)):
            stage_providers = stages[stage_index]
            stage_items, stage_error = self._search_stage_items(
                query=query,
                stage_index=stage_index,
                stage_providers=stage_providers,
            )
            if stage_error is not None:
                last_search_error = stage_error

            stage_items = [item for item in stage_items if item.score >= self._settings.min_score]
            stage_items.sort(key=lambda item: item.score, reverse=True)

            for item in stage_items:
                item_key = self._subtitle_item_key(item.provider, item.subtitle_id)
                if item_key in seen_keys:
                    continue
                seen_keys.add(item_key)

                try:
                    current_entry = self._get_cached_subtitle(item.token)
                    fetched = self._fetch_entry_once(
                        response_token=response_token,
                        entry=current_entry,
                        filename=filename,
                        requires_chinese=requires_chinese,
                    )
                except Exception as exc:
                    last_download_error = exc
                    continue

                return fetched

        if last_download_error is not None:
            raise SubtitleDownloadError(str(last_download_error)) from last_download_error
        if last_search_error is not None:
            raise SubtitleDownloadError(str(last_search_error)) from last_search_error
        return None

    def _fetch_entry_once(
        self,
        *,
        response_token: str,
        entry: CachedSubtitle,
        filename: str | None,
        requires_chinese: bool,
    ) -> InMemorySubtitle:
        if entry.kind == "direct":
            return self._fetch_direct_entry_once(
                response_token=response_token,
                entry=entry,
                filename=filename,
                requires_chinese=requires_chinese,
            )

        if entry.kind == "subliminal":
            return self._fetch_subliminal_entry_once(
                response_token=response_token,
                entry=entry,
                filename=filename,
                requires_chinese=requires_chinese,
            )

        raise SubtitleDownloadError("unknown subtitle cache entry")

    def _fetch_direct_entry_once(
        self,
        *,
        response_token: str,
        entry: CachedSubtitle,
        filename: str | None,
        requires_chinese: bool,
    ) -> InMemorySubtitle:
        candidate = entry.payload
        if not isinstance(candidate, DirectSubtitleCandidate):
            raise SubtitleDownloadError("invalid direct subtitle payload")

        downloaded = self._chinese_provider.download(candidate, query=entry.query)
        if not downloaded.content:
            raise SubtitleDownloadError("subtitle content is empty")
        if requires_chinese and not self._content_has_chinese_text(downloaded.content):
            raise SubtitleDownloadError("downloaded subtitle content does not contain Chinese text")

        return self._build_in_memory_direct(
            token=response_token,
            query=entry.query,
            candidate=candidate,
            downloaded=downloaded,
            filename=filename,
        )

    def _fetch_subliminal_entry_once(
        self,
        *,
        response_token: str,
        entry: CachedSubtitle,
        filename: str | None,
        requires_chinese: bool,
    ) -> InMemorySubtitle:
        subtitle = entry.payload
        provider = str(getattr(subtitle, "provider_name", "unknown"))

        try:
            self._backend.download_subtitles(
                [subtitle],
                providers=[provider],
                provider_configs=self._settings.provider_configs,
            )
        except Exception as exc:
            raise SubtitleDownloadError(f"subtitle download failed: {exc}") from exc

        content = getattr(subtitle, "content", None)
        if not content:
            text = getattr(subtitle, "text", "")
            if text:
                content = str(text).encode("utf-8")

        if not content:
            raise SubtitleDownloadError("subtitle content is empty")
        if requires_chinese and not self._content_has_chinese_text(content):
            raise SubtitleDownloadError("downloaded subtitle content does not contain Chinese text")

        subtitle_id = self._subtitle_id(subtitle)
        subtitle_format = str(getattr(subtitle, "subtitle_format", "srt") or "srt")

        resolved_filename = filename or self._build_filename(
            query=entry.query,
            provider=provider,
            language=language_to_code(getattr(subtitle, "language", None)),
            subtitle_format=subtitle_format,
        )

        return InMemorySubtitle(
            token=response_token,
            subtitle_id=subtitle_id,
            provider=provider,
            filename=resolved_filename,
            subtitle_format=subtitle_format,
            content=content,
        )

    def _search_with_subliminal_providers(
        self,
        *,
        query: SearchRequest,
        providers: list[str],
        stage_index: int | None = None,
    ) -> list[SubtitleSearchItem]:
        languages = parse_languages(query.languages)
        video = self._build_video(query)

        try:
            subtitle_map = self._backend.list_subtitles(
                {video},
                languages,
                providers=providers,
                provider_configs=self._settings.provider_configs,
            )
        except Exception as exc:
            joined = ",".join(providers)
            raise SubtitleSearchError(f"subliminal provider search failed ({joined}): {exc}") from exc

        subtitles = subtitle_map.get(video, [])
        results: list[SubtitleSearchItem] = []

        for subtitle in subtitles:
            token = uuid4().hex
            with self._lock:
                self._cache[token] = CachedSubtitle(
                    kind="subliminal",
                    payload=subtitle,
                    query=query,
                    created_at=self._now_fn(),
                    stage_index=stage_index,
                )

            provider = str(getattr(subtitle, "provider_name", "unknown"))
            subtitle_id = self._subtitle_id(subtitle)
            language = language_to_code(getattr(subtitle, "language", None))
            subtitle_format = str(getattr(subtitle, "subtitle_format", "srt") or "srt")

            try:
                matches = sorted(str(item) for item in subtitle.get_matches(video))
            except Exception:
                matches = []

            try:
                score = int(self._backend.compute_score(subtitle, video, hearing_impaired=False))
            except Exception:
                score = 0

            results.append(
                SubtitleSearchItem(
                    token=token,
                    provider=provider,
                    subtitle_id=subtitle_id,
                    title=self._subtitle_title(query, subtitle),
                    language=language,
                    score=score,
                    matches=matches,
                    hearing_impaired=getattr(subtitle, "hearing_impaired", None),
                    page_link=getattr(subtitle, "page_link", None),
                    subtitle_format=subtitle_format,
                    download_url=f"/api/v1/subtitles/fetch/{token}",
                )
            )

        return results

    def _get_cached_subtitle(self, token: str) -> CachedSubtitle:
        self._cleanup_cache()

        with self._lock:
            entry = self._cache.get(token)

        if not entry:
            raise SubtitleNotFoundError("subtitle token not found or expired")

        return entry

    def _cleanup_cache(self) -> None:
        ttl = timedelta(seconds=self._settings.token_ttl_seconds)
        now = self._now_fn()

        with self._lock:
            expired_tokens = [
                token
                for token, entry in self._cache.items()
                if now - entry.created_at > ttl
            ]

            for token in expired_tokens:
                self._cache.pop(token, None)

    @staticmethod
    def _subtitle_id(subtitle: Any) -> str:
        raw_id = getattr(subtitle, "id", None)
        if raw_id:
            return str(raw_id)

        raw_id = getattr(subtitle, "subtitle_id", None)
        if raw_id:
            return str(raw_id)

        return "unknown"

    @staticmethod
    def _subtitle_title(query: SearchRequest, subtitle: Any) -> str:
        release = getattr(subtitle, "release_info", None)
        if release:
            return str(release)
        return query.title

    def _build_in_memory_direct(
        self,
        *,
        token: str,
        query: SearchRequest,
        candidate: DirectSubtitleCandidate,
        downloaded: DownloadedSubtitle,
        filename: str | None,
    ) -> InMemorySubtitle:
        subtitle_format = (downloaded.subtitle_format or candidate.subtitle_format or "srt").lower()
        language = downloaded.language or candidate.language or "zh"

        suggested_name = downloaded.filename
        if suggested_name:
            suggested_name = self._sanitize_filename(unquote(suggested_name))

        resolved_filename = filename or suggested_name or self._build_filename(
            query=query,
            provider=candidate.provider,
            language=language,
            subtitle_format=subtitle_format,
        )
        resolved_filename = self._ensure_extension(resolved_filename, subtitle_format)

        return InMemorySubtitle(
            token=token,
            subtitle_id=candidate.subtitle_id,
            provider=candidate.provider,
            filename=resolved_filename,
            subtitle_format=subtitle_format,
            content=downloaded.content,
        )

    @staticmethod
    def _candidate_key(candidate: DirectSubtitleCandidate) -> tuple[str, str, str]:
        return candidate.provider, candidate.subtitle_id, candidate.download_url

    @staticmethod
    def _subtitle_item_key(provider: str, subtitle_id: str) -> tuple[str, str]:
        return provider.strip().lower(), subtitle_id.strip()

    def _dedupe_search_items(self, items: list[SubtitleSearchItem]) -> list[SubtitleSearchItem]:
        seen: set[tuple[str, str]] = set()
        deduped: list[SubtitleSearchItem] = []
        for item in items:
            key = self._subtitle_item_key(item.provider, item.subtitle_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _entry_key(self, entry: CachedSubtitle) -> tuple[str, str] | None:
        if entry.kind == "direct" and isinstance(entry.payload, DirectSubtitleCandidate):
            return self._subtitle_item_key(entry.payload.provider, entry.payload.subtitle_id)
        if entry.kind == "subliminal":
            provider = str(getattr(entry.payload, "provider_name", "unknown"))
            subtitle_id = self._subtitle_id(entry.payload)
            return self._subtitle_item_key(provider, subtitle_id)
        return None

    def _resolve_stage_index(self, entry: CachedSubtitle) -> int:
        stages = self._settings.provider_stage_list
        if not stages:
            return 0

        if entry.stage_index is not None and 0 <= entry.stage_index < len(stages):
            return entry.stage_index

        key = self._entry_key(entry)
        provider = key[0] if key else ""
        if provider:
            for index, stage in enumerate(stages):
                if any(item.strip().lower() == provider for item in stage):
                    return index
        return 0

    def _direct_fallback_candidates(
        self,
        *,
        query: SearchRequest,
        exclude: set[tuple[str, str, str]],
    ) -> list[DirectSubtitleCandidate]:
        query_signature = query.model_dump(mode="json")
        seen = set(exclude)
        candidates: list[DirectSubtitleCandidate] = []

        with self._lock:
            entries = list(self._cache.values())

        for entry in entries:
            if entry.kind != "direct":
                continue
            if entry.query.model_dump(mode="json") != query_signature:
                continue

            payload = entry.payload
            if not isinstance(payload, DirectSubtitleCandidate):
                continue

            key = self._candidate_key(payload)
            if key in seen:
                continue

            seen.add(key)
            candidates.append(payload)

        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates

    @staticmethod
    def _requires_chinese_subtitle(requested_languages: list[str]) -> bool:
        normalized = {item.strip().lower() for item in requested_languages if item and item.strip()}
        if not normalized:
            return True

        chinese_aliases = {
            "zh",
            "zh-cn",
            "zh-tw",
            "zh-hans",
            "zh-hant",
            "chs",
            "cht",
            "chi",
            "zho",
        }
        return any(item in chinese_aliases for item in normalized)

    @staticmethod
    def _decode_subtitle_text(content: bytes) -> str:
        encodings = (
            "utf-8-sig",
            "utf-16",
            "utf-16le",
            "utf-16be",
            "gb18030",
            "cp936",
            "big5",
            "cp950",
        )

        for encoding in encodings:
            try:
                return content.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue

        return content.decode("utf-8", errors="ignore")

    @classmethod
    def _content_has_chinese_text(cls, content: bytes) -> bool:
        text = cls._decode_subtitle_text(content)
        if not text:
            return False

        chinese_count = len(re.findall(r"[\u3400-\u9fff]", text))
        if chinese_count >= 20:
            return True
        if chinese_count == 0:
            return False

        visible_count = len(re.findall(r"[A-Za-z\u3400-\u9fff]", text))
        if visible_count <= 0:
            return False

        return (chinese_count / visible_count) >= 0.01

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", name)
        return cleaned.strip("._") or "subtitle"

    @staticmethod
    def _ensure_extension(name: str, subtitle_format: str) -> str:
        suffix = Path(name).suffix.lower()
        desired_suffix = f".{subtitle_format.lower().strip()}"
        if suffix:
            return name
        return f"{name}{desired_suffix}"

    def _build_filename(
        self,
        *,
        query: SearchRequest,
        provider: str,
        language: str,
        subtitle_format: str,
    ) -> str:
        base = query.title
        if query.media_type == "tv":
            season = query.season or 1
            episode = query.episode or 1
            base = f"{base}.S{season:02d}E{episode:02d}"
        elif query.year:
            base = f"{base}.{query.year}"

        ext = subtitle_format.lower() if subtitle_format else "srt"
        ext = self._sanitize_filename(ext)

        merged = ".".join(
            [
                self._sanitize_filename(base),
                self._sanitize_filename(language),
                self._sanitize_filename(provider),
            ]
        )

        return f"{merged}.{ext}"

    def _build_video(self, query: SearchRequest) -> Any:
        imdb_id = self._normalize_imdb_for_provider(query.imdb_id)

        if query.media_type == "tv":
            season = query.season or 1
            episode = query.episode or 1

            kwargs: dict[str, Any] = {}
            if query.year:
                kwargs["year"] = query.year
            if imdb_id:
                kwargs["series_imdb_id"] = imdb_id
            if query.tmdb_id:
                kwargs["series_tmdb_id"] = query.tmdb_id

            return Episode(
                name=f"{query.title}.S{season:02d}E{episode:02d}",
                series=query.title,
                season=season,
                episodes=episode,
                **kwargs,
            )

        kwargs = {}
        if query.year:
            kwargs["year"] = query.year
        if imdb_id:
            kwargs["imdb_id"] = imdb_id
        if query.tmdb_id:
            kwargs["tmdb_id"] = query.tmdb_id

        return Movie(
            name=query.title,
            title=query.title,
            **kwargs,
        )

    @staticmethod
    def _normalize_imdb_for_provider(imdb_id: str | None) -> str | None:
        if not imdb_id:
            return None
        cleaned = imdb_id.strip()
        if not cleaned:
            return None
        if cleaned.startswith("tt"):
            return cleaned
        if cleaned.isdigit():
            return f"tt{cleaned}"
        return cleaned
