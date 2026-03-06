from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.errors import SubtitleDownloadError, SubtitleNotFoundError
from app.models import SearchRequest
from app.service import SubtitleService

from .fakes import FakeChineseProvider, make_candidate


def _settings(tmp_path):
    return Settings(
        default_providers="assrt,subhd",
        default_languages="zh-cn,zh-tw",
        subtitle_output_dir=tmp_path,
        token_ttl_seconds=3600,
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
