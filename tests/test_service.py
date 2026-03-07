from __future__ import annotations

import types
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.errors import SubtitleDownloadError, SubtitleNotFoundError
from app.models import SearchRequest, SubtitleSearchItem
from app.service import CachedSubtitle, ProviderPerformanceStats, SubtitleService

from .fakes import FakeChineseProvider, make_candidate


class _FakeSubliminalSubtitle:
    provider_name = "podnapisi"
    id = "fallback-1"
    subtitle_format = "srt"
    language = "zh"
    content: bytes | None = None

    @staticmethod
    def get_matches(_video):
        return set()


class _FakeBackend:
    @staticmethod
    def list_subtitles(*args, **kwargs):
        return {}

    @staticmethod
    def download_subtitles(subtitles, *, providers, provider_configs):
        for subtitle in subtitles:
            subtitle.content = "1\n00:00:00,000 --> 00:00:01,000\n回退中文字幕\n".encode("utf-8")

    @staticmethod
    def compute_score(*args, **kwargs):
        return 88


class _MemorySubtitle:
    def __init__(
        self,
        *,
        provider_name: str,
        subtitle_id: str,
        score: int,
        content: bytes | None,
        language: str = "zh",
        subtitle_format: str = "srt",
    ) -> None:
        self.provider_name = provider_name
        self.id = subtitle_id
        self.subtitle_format = subtitle_format
        self.language = language
        self.content = content
        self._score = score

    @staticmethod
    def get_matches(_video):
        return {"episode", "year"}


class _StagedBackend:
    def __init__(self, subtitles_by_provider: dict[str, list[_MemorySubtitle]]) -> None:
        self._subtitles_by_provider = subtitles_by_provider

    def list_subtitles(self, videos, languages, *, providers, provider_configs):
        video = next(iter(videos))
        subtitles = []
        for provider in providers:
            subtitles.extend(self._subtitles_by_provider.get(provider, []))
        return {video: subtitles}

    @staticmethod
    def download_subtitles(subtitles, *, providers, provider_configs):
        # No-op: content is pre-baked in _MemorySubtitle.
        for _subtitle in subtitles:
            continue

    @staticmethod
    def compute_score(subtitle, video, hearing_impaired=False):
        return int(getattr(subtitle, "_score", 0))


def _settings(tmp_path):
    return Settings(
        default_providers="assrt,subhd",
        default_languages="zh-cn,zh-tw",
        subtitle_output_dir=tmp_path,
        token_ttl_seconds=3600,
        enable_subliminal_fallback=False,
    )


def test_search_sorts_and_limits(tmp_path):
    provider = FakeChineseProvider(
        [
            make_candidate(subtitle_id="s-low", score=50),
            make_candidate(subtitle_id="s-high", score=120),
        ]
    )
    service = SubtitleService(settings=_settings(tmp_path), chinese_provider=provider)

    result = service.search(
        SearchRequest(
            title="匹兹堡医护前线",
            media_type="tv",
            season=2,
            episode=5,
            languages=["zh-cn", "zh-tw"],
            limit=1,
        )
    )

    assert result.total == 2
    assert len(result.items) == 1
    assert result.items[0].subtitle_id == "s-high"
    assert result.items[0].provider == "assrt"


def test_download_to_disk_creates_file(tmp_path):
    provider = FakeChineseProvider([make_candidate(subtitle_id="s-1", score=88)])
    service = SubtitleService(settings=_settings(tmp_path), chinese_provider=provider)

    result = service.search(
        SearchRequest(
            title="匹兹堡医护前线",
            media_type="tv",
            season=2,
            episode=5,
            languages=["zh-cn"],
            limit=5,
        )
    )

    token = result.items[0].token
    downloaded = service.download_to_disk(token)

    assert downloaded.size > 0
    assert downloaded.filename.endswith(".srt")
    assert tmp_path.joinpath(downloaded.filename).exists()


def test_download_to_disk_skips_english_candidate_and_uses_chinese_alternative(tmp_path):
    provider = FakeChineseProvider(
        [
            make_candidate(subtitle_id="s-en", score=200),
            make_candidate(subtitle_id="s-zh", score=120),
        ],
        content_by_subtitle_id={
            "s-en": "1\n00:00:00,000 --> 00:00:01,000\nHello, doctor.\n".encode("utf-16"),
            "s-zh": "1\n00:00:00,000 --> 00:00:01,000\n你好，医生。\n".encode("utf-8"),
        },
    )
    service = SubtitleService(settings=_settings(tmp_path), chinese_provider=provider)

    result = service.search(
        SearchRequest(
            title="匹兹堡医护前线",
            media_type="tv",
            season=2,
            episode=5,
            languages=["zh-cn", "zh-tw"],
            limit=1,
        )
    )

    token = result.items[0].token
    downloaded = service.download_to_disk(token)

    assert downloaded.subtitle_id == "s-zh"
    content = tmp_path.joinpath(downloaded.filename).read_bytes()
    assert "你好".encode("utf-8") in content


def test_download_rejects_sparse_chinese_and_uses_better_candidate(tmp_path):
    sparse = (
        "1\n00:00:00,000 --> 00:00:01,000\nHello from the archive candidate.\n"
        "2\n00:00:01,000 --> 00:00:02,000\nStill English lines only.\n"
        "3\n00:00:02,000 --> 00:00:03,000\nAnother English line.\n"
        "4\n00:00:03,000 --> 00:00:04,000\n这条仅用于误导\n"
    ).encode("utf-8")
    provider = FakeChineseProvider(
        [
            make_candidate(subtitle_id="s-sparse", score=220),
            make_candidate(subtitle_id="s-zh", score=120),
        ],
        content_by_subtitle_id={
            "s-sparse": sparse,
            "s-zh": (
                "1\n00:00:00,000 --> 00:00:01,000\n你好，医生。\n"
                "2\n00:00:01,000 --> 00:00:02,000\n今天情况怎么样？\n"
            ).encode("utf-8"),
        },
    )
    service = SubtitleService(settings=_settings(tmp_path), chinese_provider=provider)

    result = service.search(
        SearchRequest(
            title="匹兹堡医护前线",
            media_type="tv",
            season=2,
            episode=5,
            languages=["zh-cn", "zh-tw"],
            limit=1,
        )
    )

    token = result.items[0].token
    downloaded = service.download_to_disk(token)

    assert downloaded.subtitle_id == "s-zh"
    content = tmp_path.joinpath(downloaded.filename).read_bytes()
    assert "你好".encode("utf-8") in content


def test_chinese_confidence_accepts_bilingual_content(tmp_path):
    provider = FakeChineseProvider([make_candidate(subtitle_id="s-1", score=88)])
    service = SubtitleService(settings=_settings(tmp_path), chinese_provider=provider)

    content = (
        "1\n00:00:00,000 --> 00:00:02,000\n你好，医生。 Hello doctor.\n"
        "2\n00:00:02,000 --> 00:00:04,000\n我们继续。 Let's continue.\n"
    ).encode("utf-8")
    passed, confidence = service._verify_chinese_content(content)

    assert passed is True
    assert confidence.score >= service._settings.chinese_confidence_threshold


def test_download_to_disk_raises_when_all_candidates_are_non_chinese(tmp_path):
    provider = FakeChineseProvider(
        [make_candidate(subtitle_id="s-en", score=88)],
        content_by_subtitle_id={
            "s-en": "1\n00:00:00,000 --> 00:00:01,000\nHello, doctor.\n".encode("utf-16"),
        },
    )
    service = SubtitleService(settings=_settings(tmp_path), chinese_provider=provider)

    result = service.search(
        SearchRequest(
            title="匹兹堡医护前线",
            media_type="tv",
            season=2,
            episode=5,
            languages=["zh-cn"],
            limit=5,
        )
    )

    token = result.items[0].token
    with pytest.raises(SubtitleDownloadError):
        service.download_to_disk(token)


def test_expired_token_raises_not_found(tmp_path):
    provider = FakeChineseProvider([make_candidate(subtitle_id="s-1", score=88)])

    now = [datetime(2026, 3, 5, 0, 0, tzinfo=timezone.utc)]

    service = SubtitleService(
        settings=Settings(
            default_providers="assrt,subhd",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            token_ttl_seconds=1,
        ),
        chinese_provider=provider,
        now_fn=lambda: now[0],
    )

    result = service.search(
        SearchRequest(
            title="匹兹堡医护前线",
            media_type="tv",
            season=2,
            episode=5,
            languages=["zh-cn"],
        )
    )
    token = result.items[0].token

    now[0] = now[0] + timedelta(seconds=5)

    with pytest.raises(SubtitleNotFoundError):
        service.fetch_to_memory(token)


def test_search_fallback_chain_tries_secondary_then_opensubtitles(tmp_path):
    provider = FakeChineseProvider([])
    service = SubtitleService(
        settings=Settings(
            default_providers="assrt,subhd",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            enable_subliminal_fallback=True,
            subliminal_fallback_providers="podnapisi,tvsubtitles,opensubtitlescom,opensubtitles",
            enable_parallel_search=False,
        ),
        chinese_provider=provider,
    )

    calls: list[list[str]] = []

    def fake_search_with_subliminal(self, *, query, providers, stage_index=None):
        calls.append(list(providers))
        return []

    service._search_with_subliminal_providers = types.MethodType(fake_search_with_subliminal, service)

    result = service.search(
        SearchRequest(
            title="短剧开始啦",
            media_type="tv",
            season=1,
            episode=3,
            languages=["zh-cn", "zh-tw"],
            limit=5,
        )
    )

    assert result.total == 0
    assert calls == [
        ["podnapisi", "tvsubtitles"],
        ["opensubtitlescom", "opensubtitles"],
    ]


def test_search_stops_before_opensubtitles_when_secondary_has_results(tmp_path):
    provider = FakeChineseProvider([])
    service = SubtitleService(
        settings=Settings(
            default_providers="assrt,subhd",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            enable_subliminal_fallback=True,
            subliminal_fallback_providers="podnapisi,tvsubtitles,opensubtitlescom,opensubtitles",
            enable_parallel_search=False,
        ),
        chinese_provider=provider,
    )

    calls: list[list[str]] = []

    def fake_search_with_subliminal(self, *, query, providers, stage_index=None):
        calls.append(list(providers))
        if providers == ["podnapisi", "tvsubtitles"]:
            return [
                SubtitleSearchItem(
                    token="t-secondary",
                    provider="podnapisi",
                    subtitle_id="secondary-1",
                    title=query.title,
                    language="zh",
                    score=66,
                    matches=[],
                    hearing_impaired=None,
                    page_link=None,
                    subtitle_format="srt",
                    download_url="/api/v1/subtitles/fetch/t-secondary",
                )
            ]
        return []

    service._search_with_subliminal_providers = types.MethodType(fake_search_with_subliminal, service)

    result = service.search(
        SearchRequest(
            title="短剧开始啦",
            media_type="tv",
            season=1,
            episode=3,
            languages=["zh-cn", "zh-tw"],
            limit=5,
        )
    )

    assert result.total == 1
    assert result.items[0].provider == "podnapisi"
    assert calls == [["podnapisi", "tvsubtitles"]]


def test_search_respects_custom_provider_stage_order(tmp_path):
    provider = FakeChineseProvider([make_candidate(subtitle_id="s-direct", score=300)])
    service = SubtitleService(
        settings=Settings(
            default_providers="assrt,subhd,subhdtw",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            enable_subliminal_fallback=True,
            provider_stage_order="opensubtitlescom,opensubtitles|assrt,subhd,subhdtw",
            min_score=0,
            enable_parallel_search=False,
        ),
        chinese_provider=provider,
    )

    calls: list[list[str]] = []

    def fake_search_with_subliminal(self, *, query, providers, stage_index=None):
        calls.append(list(providers))
        return [
            SubtitleSearchItem(
                token="t-open",
                provider="opensubtitlescom",
                subtitle_id="open-1",
                title=query.title,
                language="zh",
                score=88,
                matches=[],
                hearing_impaired=None,
                page_link=None,
                subtitle_format="srt",
                download_url="/api/v1/subtitles/fetch/t-open",
            )
        ]

    service._search_with_subliminal_providers = types.MethodType(fake_search_with_subliminal, service)

    result = service.search(
        SearchRequest(
            title="短剧开始啦",
            media_type="tv",
            season=1,
            episode=3,
            languages=["zh-cn", "zh-tw"],
            limit=5,
        )
    )

    assert result.total == 1
    assert result.items[0].provider == "opensubtitlescom"
    assert calls == [["opensubtitlescom", "opensubtitles"]]
    assert provider.search_calls == 0


def test_parallel_search_splits_subliminal_providers(tmp_path):
    provider = FakeChineseProvider([])
    service = SubtitleService(
        settings=Settings(
            default_providers="",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            provider_stage_order="opensubtitlescom,opensubtitles",
            enable_subliminal_fallback=False,
            min_score=0,
            enable_parallel_search=True,
            search_workers=4,
        ),
        chinese_provider=provider,
    )

    lock = threading.Lock()
    calls: list[str] = []

    def fake_search_with_subliminal(self, *, query, providers, stage_index=None):
        assert len(providers) == 1
        provider_name = providers[0]
        with lock:
            calls.append(provider_name)
        return [
            SubtitleSearchItem(
                token=f"t-{provider_name}",
                provider=provider_name,
                subtitle_id=f"{provider_name}-1",
                title=query.title,
                language="zh",
                score=88,
                matches=[],
                hearing_impaired=None,
                page_link=None,
                subtitle_format="srt",
                download_url=f"/api/v1/subtitles/fetch/t-{provider_name}",
            )
        ]

    service._search_with_subliminal_providers = types.MethodType(fake_search_with_subliminal, service)

    result = service.search(
        SearchRequest(
            title="短剧开始啦",
            media_type="tv",
            season=1,
            episode=3,
            languages=["zh-cn", "zh-tw"],
            limit=5,
        )
    )

    assert result.total == 2
    assert sorted(calls) == ["opensubtitles", "opensubtitlescom"]


def test_download_uses_subliminal_fallback_when_direct_candidates_fail(tmp_path):
    provider = FakeChineseProvider(
        [make_candidate(subtitle_id="s-direct", score=88)],
        content_by_subtitle_id={
            "s-direct": "1\n00:00:00,000 --> 00:00:01,000\nHello, doctor.\n".encode("utf-16"),
        },
    )
    service = SubtitleService(
        settings=Settings(
            default_providers="assrt,subhd",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            token_ttl_seconds=3600,
            enable_subliminal_fallback=True,
            subliminal_fallback_providers="podnapisi,tvsubtitles,opensubtitlescom,opensubtitles",
        ),
        backend=_FakeBackend(),
        chinese_provider=provider,
    )

    def fake_search_with_subliminal(self, *, query, providers, stage_index=None):
        subtitle = _FakeSubliminalSubtitle()
        token = "fallback-token"
        with self._lock:
            self._cache[token] = CachedSubtitle(
                kind="subliminal",
                payload=subtitle,
                query=query,
                created_at=self._now_fn(),
                stage_index=stage_index,
            )
        return [
            SubtitleSearchItem(
                token=token,
                provider="podnapisi",
                subtitle_id="fallback-1",
                title=query.title,
                language="zh",
                score=88,
                matches=[],
                hearing_impaired=None,
                page_link=None,
                subtitle_format="srt",
                download_url=f"/api/v1/subtitles/fetch/{token}",
            )
        ]

    service._search_with_subliminal_providers = types.MethodType(fake_search_with_subliminal, service)

    search_result = service.search(
        SearchRequest(
            title="短剧开始啦",
            media_type="tv",
            season=1,
            episode=3,
            languages=["zh-cn", "zh-tw"],
            limit=5,
        )
    )
    token = search_result.items[0].token
    downloaded = service.download_to_disk(token)

    assert downloaded.provider == "podnapisi"
    content = tmp_path.joinpath(downloaded.filename).read_bytes()
    assert "回退中文字幕".encode("utf-8") in content


def test_download_retries_other_candidates_in_same_stage(tmp_path):
    provider = FakeChineseProvider([make_candidate(subtitle_id="s-direct", score=80)])
    backend = _StagedBackend(
        {
            "opensubtitlescom": [
                _MemorySubtitle(
                    provider_name="opensubtitlescom",
                    subtitle_id="open-empty",
                    score=220,
                    content=None,
                ),
                _MemorySubtitle(
                    provider_name="opensubtitlescom",
                    subtitle_id="open-zh",
                    score=210,
                    content="1\n00:00:00,000 --> 00:00:01,000\n同阶段候选命中\n".encode("utf-8"),
                ),
            ]
        }
    )
    service = SubtitleService(
        settings=Settings(
            default_providers="assrt",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            provider_stage_order="opensubtitlescom|assrt",
            enable_subliminal_fallback=True,
            min_score=0,
        ),
        backend=backend,
        chinese_provider=provider,
    )

    search_result = service.search(
        SearchRequest(
            title="短剧开始啦",
            media_type="tv",
            season=1,
            episode=3,
            languages=["zh-cn", "zh-tw"],
            limit=5,
        )
    )
    token = search_result.items[0].token
    downloaded = service.download_to_disk(token)

    assert downloaded.provider == "opensubtitlescom"
    assert downloaded.subtitle_id == "open-zh"
    content = tmp_path.joinpath(downloaded.filename).read_bytes()
    assert "同阶段候选命中".encode("utf-8") in content


def test_download_prioritizes_non_subhd_candidates_before_subhd_family(tmp_path):
    provider = FakeChineseProvider(
        [
            make_candidate(subtitle_id="s-subhd", score=260, provider="subhd"),
            make_candidate(subtitle_id="s-assrt", score=180, provider="assrt"),
        ],
        content_by_subtitle_id={
            "s-assrt": "1\n00:00:00,000 --> 00:00:01,000\n优先尝试assrt成功\n".encode("utf-8"),
            "s-subhd": "1\n00:00:00,000 --> 00:00:01,000\nsubhd内容\n".encode("utf-8"),
        },
    )
    service = SubtitleService(
        settings=Settings(
            default_providers="assrt,subhd",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            token_ttl_seconds=3600,
            enable_subliminal_fallback=False,
        ),
        chinese_provider=provider,
    )

    search_result = service.search(
        SearchRequest(
            title="测试剧集",
            media_type="tv",
            season=1,
            episode=1,
            languages=["zh-cn", "zh-tw"],
            limit=1,
        )
    )
    token = search_result.items[0].token
    downloaded = service.download_to_disk(token)

    assert downloaded.provider == "assrt"
    assert provider.download_calls[0] == "s-assrt"


def test_download_falls_through_to_next_stage_when_first_stage_fails(tmp_path):
    provider = FakeChineseProvider(
        [make_candidate(subtitle_id="assrt-zh", score=180, provider="assrt")]
    )
    backend = _StagedBackend(
        {
            "opensubtitlescom": [
                _MemorySubtitle(
                    provider_name="opensubtitlescom",
                    subtitle_id="open-empty",
                    score=260,
                    content=None,
                )
            ]
        }
    )
    service = SubtitleService(
        settings=Settings(
            default_providers="assrt",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            provider_stage_order="opensubtitlescom|assrt",
            enable_subliminal_fallback=True,
            min_score=0,
        ),
        backend=backend,
        chinese_provider=provider,
    )

    search_result = service.search(
        SearchRequest(
            title="短剧开始啦",
            media_type="tv",
            season=1,
            episode=3,
            languages=["zh-cn", "zh-tw"],
            limit=5,
        )
    )
    token = search_result.items[0].token
    downloaded = service.download_to_disk(token)

    assert downloaded.provider == "assrt"
    assert downloaded.subtitle_id == "assrt-zh"
    assert provider.search_calls == 1


def test_download_error_mentions_fallback_attempts_when_exhausted(tmp_path):
    provider = FakeChineseProvider(
        [make_candidate(subtitle_id="s-direct", score=88)],
        error_by_subtitle_id={"s-direct": SubtitleDownloadError("subhd mirrors require captcha verification")},
    )
    service = SubtitleService(
        settings=Settings(
            default_providers="subhd",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            token_ttl_seconds=3600,
            enable_subliminal_fallback=True,
            subliminal_fallback_providers="podnapisi,tvsubtitles,opensubtitlescom",
        ),
        backend=_FakeBackend(),
        chinese_provider=provider,
    )

    def fake_search_with_subliminal(self, *, query, providers, stage_index=None):
        return []

    service._search_with_subliminal_providers = types.MethodType(fake_search_with_subliminal, service)

    search_result = service.search(
        SearchRequest(
            title="国宝",
            media_type="movie",
            year=2025,
            languages=["zh-cn", "zh-tw"],
            limit=5,
        )
    )
    token = search_result.items[0].token

    with pytest.raises(SubtitleDownloadError) as exc:
        service.download_to_disk(token)

    message = str(exc.value)
    assert "fallback providers attempted" in message
    assert "podnapisi,tvsubtitles,opensubtitlescom" in message


def test_is_subhd_captcha_error_supports_ajax_gzh_message(tmp_path):
    service = SubtitleService(
        settings=Settings(
            default_providers="subhd",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            token_ttl_seconds=3600,
        ),
        backend=_FakeBackend(),
        chinese_provider=FakeChineseProvider([]),
    )

    assert service._is_subhd_captcha_error(
        SubtitleDownloadError("subhd site verification required (/ajax/gzh) on subhd.tv")
    )


def test_adaptive_provider_priority_ranks_downloadable_before_undownloadable(tmp_path):
    service = SubtitleService(
        settings=Settings(
            default_providers="subhd,assrt,podnapisi",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            token_ttl_seconds=3600,
            enable_adaptive_provider_priority=True,
            provider_priority_stats_file=tmp_path / "provider-priority.json",
            provider_priority_persist_interval_seconds=0,
        ),
        backend=_FakeBackend(),
        chinese_provider=FakeChineseProvider([]),
    )

    with service._provider_stats_lock:
        service._provider_stats["assrt"] = ProviderPerformanceStats(
            search_hits=6,
            search_misses=1,
            download_successes=4,
            download_failures=1,
        )
        service._provider_stats["subhd"] = ProviderPerformanceStats(
            search_hits=8,
            search_misses=1,
            download_successes=0,
            download_failures=8,
        )

    ranked = service._rank_stage_providers(["subhd", "assrt", "podnapisi"])
    assert ranked == ["assrt", "subhd", "podnapisi"]


def test_adaptive_direct_candidate_priority_prefers_downloadable_provider(tmp_path):
    service = SubtitleService(
        settings=Settings(
            default_providers="subhd,assrt",
            default_languages="zh-cn,zh-tw",
            subtitle_output_dir=tmp_path,
            token_ttl_seconds=3600,
            enable_adaptive_provider_priority=True,
            provider_priority_stats_file=tmp_path / "provider-priority.json",
            provider_priority_persist_interval_seconds=0,
        ),
        backend=_FakeBackend(),
        chinese_provider=FakeChineseProvider([]),
    )

    with service._provider_stats_lock:
        service._provider_stats["assrt"] = ProviderPerformanceStats(
            search_hits=10,
            download_successes=5,
            download_failures=1,
        )
        service._provider_stats["subhd"] = ProviderPerformanceStats(
            search_hits=10,
            download_successes=0,
            download_failures=10,
        )

    prioritized = service._prioritize_direct_candidates(
        [
            make_candidate(subtitle_id="s-subhd", score=320, provider="subhd"),
            make_candidate(subtitle_id="s-assrt", score=180, provider="assrt"),
        ]
    )
    assert prioritized[0].provider == "assrt"
