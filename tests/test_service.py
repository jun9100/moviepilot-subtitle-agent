from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.errors import SubtitleDownloadError, SubtitleNotFoundError
from app.models import SearchRequest, SubtitleSearchItem
from app.service import CachedSubtitle, SubtitleService

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
        ),
        chinese_provider=provider,
    )

    calls: list[list[str]] = []

    def fake_search_with_subliminal(self, *, query, providers):
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
        ),
        chinese_provider=provider,
    )

    calls: list[list[str]] = []

    def fake_search_with_subliminal(self, *, query, providers):
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
        ),
        chinese_provider=provider,
    )

    calls: list[list[str]] = []

    def fake_search_with_subliminal(self, *, query, providers):
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

    def fake_search_with_subliminal(self, *, query, providers):
        subtitle = _FakeSubliminalSubtitle()
        token = "fallback-token"
        with self._lock:
            self._cache[token] = CachedSubtitle(
                kind="subliminal",
                payload=subtitle,
                query=query,
                created_at=self._now_fn(),
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
