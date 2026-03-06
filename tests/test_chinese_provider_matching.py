from __future__ import annotations

from app.chinese_provider import ChineseSubtitleProvider, DirectSubtitleCandidate
from app.models import SearchRequest


def _provider() -> ChineseSubtitleProvider:
    return ChineseSubtitleProvider(timeout_seconds=5)


def _query() -> SearchRequest:
    return SearchRequest(
        title="コントが始まる",
        media_type="tv",
        year=2021,
        season=1,
        episode=6,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )


def test_pick_extracted_subtitle_prefers_target_episode(tmp_path):
    root = tmp_path / "subs"
    root.mkdir(parents=True, exist_ok=True)

    (root / "Konto.S01E01.zh-cn.srt").write_text("ep1", encoding="utf-8")
    (root / "Konto.S01E06.zh-cn.srt").write_text("ep6", encoding="utf-8")
    (root / "Konto.S01E07.zh-cn.srt").write_text("ep7", encoding="utf-8")

    selected = _provider()._pick_extracted_subtitle(root, _query())

    assert selected is not None
    assert selected.name == "Konto.S01E06.zh-cn.srt"


def test_candidate_without_episode_is_rejected_for_tv_episode_query():
    candidate = DirectSubtitleCandidate(
        provider="assrt",
        subtitle_id="x-1",
        title="コントが始まる",
        release_name="官方中字",
        language="zh-cn",
        subtitle_format="srt",
        download_url="https://assrt.net/download/x-1/demo.srt",
        page_link="https://assrt.net/xml/sub/1/1.xml",
        language_tags=["zh-cn"],
        matches=[],
        score=0,
    )

    assert _provider()._candidate_matches_query(candidate, _query()) is False


def test_season_pack_archive_is_allowed_for_tv_episode_query():
    candidate = DirectSubtitleCandidate(
        provider="assrt",
        subtitle_id="x-2",
        title="コントが始まる",
        release_name="コントが始まる S01 Complete 官方中字",
        language="zh-cn",
        subtitle_format="srt",
        download_url="https://assrt.net/download/x-2/demo.zip",
        page_link="https://assrt.net/xml/sub/2/2.xml",
        language_tags=["zh-cn"],
        matches=[],
        score=0,
    )

    assert _provider()._candidate_matches_query(candidate, _query()) is True


def test_in_progress_archive_is_allowed_for_tv_episode_query():
    candidate = DirectSubtitleCandidate(
        provider="assrt",
        subtitle_id="x-3",
        title="コントが始まる",
        release_name="コントが始まる S01 更新至E06 官方中字",
        language="zh-cn",
        subtitle_format="srt",
        download_url="https://assrt.net/download/x-3/demo.zip",
        page_link="https://assrt.net/xml/sub/3/3.xml",
        language_tags=["zh-cn"],
        matches=[],
        score=0,
    )

    assert _provider()._candidate_matches_query(candidate, _query()) is True


def test_archive_with_non_matching_episode_is_rejected():
    candidate = DirectSubtitleCandidate(
        provider="assrt",
        subtitle_id="x-4",
        title="コントが始まる",
        release_name="コントが始まる S01E08 官方中字",
        language="zh-cn",
        subtitle_format="srt",
        download_url="https://assrt.net/download/x-4/demo.zip",
        page_link="https://assrt.net/xml/sub/4/4.xml",
        language_tags=["zh-cn"],
        matches=[],
        score=0,
    )

    assert _provider()._candidate_matches_query(candidate, _query()) is False


def test_ambiguous_archive_without_title_or_episode_is_rejected():
    candidate = DirectSubtitleCandidate(
        provider="assrt",
        subtitle_id="x-5",
        title="官方中字压缩包",
        release_name="S01 字幕打包",
        language="zh-cn",
        subtitle_format="srt",
        download_url="https://assrt.net/download/x-5/demo.zip",
        page_link="https://assrt.net/xml/sub/5/5.xml",
        language_tags=["zh-cn"],
        matches=[],
        score=0,
    )

    assert _provider()._candidate_matches_query(candidate, _query()) is False
