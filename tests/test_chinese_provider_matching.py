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


def test_subhd_season_level_candidate_can_be_used_for_episode_query():
    candidate = DirectSubtitleCandidate(
        provider="subhd",
        subtitle_id="aCQ07t",
        title="短剧开始啦",
        release_name="Life's Punchline.S01.WEB-DL.KKTV",
        language="zh",
        subtitle_format="srt",
        download_url="https://subhd.tv/down/aCQ07t",
        page_link="https://subhd.tv/a/aCQ07t",
        language_tags=["zh-cn", "zh-tw"],
        matches=[],
        score=0,
    )

    subhd_query = SearchRequest(
        title="短剧开始啦",
        media_type="tv",
        year=2021,
        season=1,
        episode=3,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )

    assert _provider()._candidate_matches_query(candidate, subhd_query) is True


def test_subhdtw_season_level_candidate_can_be_used_for_episode_query():
    candidate = DirectSubtitleCandidate(
        provider="subhdtw",
        subtitle_id="aCQ07t",
        title="短剧开始啦",
        release_name="Life's Punchline.S01.WEB-DL.KKTV",
        language="zh",
        subtitle_format="srt",
        download_url="https://subhdtw.com/down/aCQ07t",
        page_link="https://subhdtw.com/a/aCQ07t",
        language_tags=["zh-cn", "zh-tw"],
        matches=[],
        score=0,
    )

    subhd_query = SearchRequest(
        title="短剧开始啦",
        media_type="tv",
        year=2021,
        season=1,
        episode=3,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )

    assert _provider()._candidate_matches_query(candidate, subhd_query) is True


def test_tv_query_rejects_movie_like_candidate_without_episode_markers():
    candidate = DirectSubtitleCandidate(
        provider="subhd",
        subtitle_id="N2tccz",
        title="国宝",
        release_name="国宝.Kokuho.2025.1080p.WEBRip.x264.AAC.2.0-CabPro",
        language="zh-cn",
        subtitle_format="srt",
        download_url="https://subhd.tv/down/N2tccz",
        page_link="https://subhd.tv/a/N2tccz",
        language_tags=["zh-cn"],
        matches=["resolution"],
        score=0,
    )
    tv_query = SearchRequest(
        title="国宝",
        media_type="tv",
        year=2025,
        season=1,
        episode=1,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )

    assert _provider()._candidate_matches_query(candidate, tv_query) is False


def test_tv_query_rejects_unrelated_season_only_candidate_with_low_overlap():
    candidate = DirectSubtitleCandidate(
        provider="assrt",
        subtitle_id="assrt-s01-only",
        title="Our Unwritten Seoul",
        release_name="Our.Unwritten.Seoul.S01.720p.WEB.Korean.H264-JFF",
        language="zh",
        subtitle_format="srt",
        download_url="https://assrt.net/download/123/demo.zip",
        page_link="https://assrt.net/xml/sub/123/123.xml",
        language_tags=["zh-cn", "zh-tw"],
        matches=[],
        score=0,
    )
    tv_query = SearchRequest(
        title="国宝",
        media_type="tv",
        year=2025,
        season=1,
        episode=1,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )

    assert _provider()._candidate_matches_query(candidate, tv_query) is False


def test_tv_query_keeps_exact_episode_candidate_even_when_title_is_english():
    candidate = DirectSubtitleCandidate(
        provider="assrt",
        subtitle_id="assrt-s01e03",
        title="Life's Punchline",
        release_name="Life.s.Punchline.S01E03.1080p.WEB-DL",
        language="zh",
        subtitle_format="srt",
        download_url="https://assrt.net/download/456/demo.srt",
        page_link="https://assrt.net/xml/sub/456/456.xml",
        language_tags=["zh-cn", "zh-tw"],
        matches=[],
        score=0,
    )
    tv_query = SearchRequest(
        title="短剧开始啦",
        media_type="tv",
        year=2021,
        season=1,
        episode=3,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )

    assert _provider()._candidate_matches_query(candidate, tv_query) is True


def test_search_ignores_assrt_errors_and_returns_empty(monkeypatch):
    provider = _provider()

    def fake_search_assrt(_keyword: str):
        raise RuntimeError("ssl eof")

    monkeypatch.setattr(provider, "_search_assrt", fake_search_assrt)

    results = provider.search(_query(), providers=["assrt"])
    assert results == []


def test_movie_query_rejects_series_pack_candidate():
    candidate = DirectSubtitleCandidate(
        provider="subhd",
        subtitle_id="kAx98K",
        title="护宝寻踪",
        release_name="The Lost National Treasure S01 (2025) WEB 简繁字幕",
        language="zh",
        subtitle_format="srt",
        download_url="https://subhd.tv/down/kAx98K",
        page_link="https://subhd.tv/a/kAx98K",
        language_tags=["zh-cn", "zh-tw"],
        matches=[],
        score=0,
    )
    movie_query = SearchRequest(
        title="国宝",
        media_type="movie",
        year=2025,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )

    assert _provider()._candidate_matches_query(candidate, movie_query) is False


def test_movie_scoring_prefers_movie_release_over_series_pack():
    provider = _provider()
    movie_query = SearchRequest(
        title="国宝",
        media_type="movie",
        year=2025,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )
    series_pack = DirectSubtitleCandidate(
        provider="subhd",
        subtitle_id="kAx98K",
        title="护宝寻踪",
        release_name="The Lost National Treasure S01 (2025) WEB 简繁字幕",
        language="zh",
        subtitle_format="srt",
        download_url="https://subhd.tv/down/kAx98K",
        page_link="https://subhd.tv/a/kAx98K",
        language_tags=["zh-cn", "zh-tw"],
        matches=[],
        score=0,
    )
    movie_release = DirectSubtitleCandidate(
        provider="subhd",
        subtitle_id="N2tccz",
        title="国宝",
        release_name="国宝.Kokuho.2025.1080p.WEBRip.x264.AAC.2.0-CabPro",
        language="zh-cn",
        subtitle_format="srt",
        download_url="https://subhd.tv/down/N2tccz",
        page_link="https://subhd.tv/a/N2tccz",
        language_tags=["zh-cn"],
        matches=["resolution"],
        score=0,
    )

    assert provider._score_candidate(movie_release, movie_query) > provider._score_candidate(series_pack, movie_query)


def test_movie_query_rejects_unrelated_chinese_title_with_zero_overlap():
    candidate = DirectSubtitleCandidate(
        provider="subhd",
        subtitle_id="x-cn-zero",
        title="护宝寻踪",
        release_name="护宝寻踪 S01 官方简繁字幕",
        language="zh",
        subtitle_format="srt",
        download_url="https://subhd.tv/down/x-cn-zero",
        page_link="https://subhd.tv/a/x-cn-zero",
        language_tags=["zh-cn", "zh-tw"],
        matches=[],
        score=0,
    )
    movie_query = SearchRequest(
        title="国宝",
        media_type="movie",
        year=2025,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )

    assert _provider()._candidate_matches_query(candidate, movie_query) is False


def test_movie_query_keeps_english_candidate_for_chinese_query():
    candidate = DirectSubtitleCandidate(
        provider="assrt",
        subtitle_id="x-en-only",
        title="Kokuho",
        release_name="Kokuho.2025.1080p.WEBRip.x264.AAC.2.0-CabPro",
        language="zh-cn",
        subtitle_format="srt",
        download_url="https://assrt.net/download/x-en-only/demo.srt",
        page_link="https://assrt.net/xml/sub/1/1.xml",
        language_tags=["zh-cn"],
        matches=["resolution"],
        score=0,
    )
    movie_query = SearchRequest(
        title="国宝",
        media_type="movie",
        year=2025,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )

    assert _provider()._candidate_matches_query(candidate, movie_query) is True
