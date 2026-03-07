from __future__ import annotations

import types

import pytest

from app.chinese_provider import SUBHD_MIRRORS, ChineseSubtitleProvider, DirectSubtitleCandidate, DownloadedSubtitle
from app.errors import SubtitleDownloadError
from app.models import SearchRequest


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        json_data: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content
        self.headers = headers or {}
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("json not provided")
        return self._json_data


def _provider() -> ChineseSubtitleProvider:
    return ChineseSubtitleProvider(timeout_seconds=5)


def _query() -> SearchRequest:
    return SearchRequest(
        title="短剧开始啦",
        media_type="tv",
        year=2021,
        season=1,
        episode=3,
        languages=["zh-cn", "zh-tw"],
        limit=10,
    )


def test_search_subhd_parses_candidates(monkeypatch):
    html = """
    <div class="bg-white shadow-sm rounded-3 mb-4">
      <a class="link-dark align-middle" href="/a/aCQ07t">短剧开始啦</a>
      <div class="view-text"><a href="/a/aCQ07t">Life's Punchline.S01.WEB-DL.KKTV</a></div>
      <div class="text-truncate py-2 f11">
        <span>官方字幕</span><span>简体</span><span>繁体</span><span>SRT</span>
      </div>
    </div>
    """
    provider = _provider()
    monkeypatch.setattr(provider._session, "get", lambda *args, **kwargs: _FakeResponse(status_code=200, text=html))

    results = provider._search_subhd("短剧开始啦")
    assert len(results) == 1

    item = results[0]
    assert item.provider == "subhd"
    assert item.subtitle_id == "aCQ07t"
    assert item.language == "zh"
    assert item.subtitle_format == "srt"
    assert item.download_url.endswith("/down/aCQ07t")


def test_search_subhdtw_parses_candidates(monkeypatch):
    html = """
    <div class="bg-white shadow-sm rounded-3 mb-4">
      <a class="link-dark align-middle" href="/a/aCQ07t">短剧开始啦</a>
      <div class="view-text"><a href="/a/aCQ07t">Life's Punchline.S01.WEB-DL.KKTV</a></div>
      <div class="text-truncate py-2 f11">
        <span>官方字幕</span><span>繁体</span><span>SRT</span>
      </div>
    </div>
    """
    provider = _provider()
    monkeypatch.setattr(provider._session, "get", lambda *args, **kwargs: _FakeResponse(status_code=200, text=html))

    results = provider._search_subhdtw("短剧开始啦")
    assert len(results) == 1
    item = results[0]
    assert item.provider == "subhdtw"
    assert item.page_link.startswith("https://subhdtw.com")


def test_dedupe_merges_subhd_and_subhdtw_same_sid():
    candidates = [
        DirectSubtitleCandidate(
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
        ),
        DirectSubtitleCandidate(
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
        ),
    ]

    deduped = ChineseSubtitleProvider._dedupe_candidates(candidates)
    assert len(deduped) == 1


def test_download_subhd_success(monkeypatch):
    provider = _provider()
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
    final_url = "https://dl.subhd.tv/demo.srt"
    subtitle_content = b"1\n00:00:00,000 --> 00:00:01,000\n\xe4\xbd\xa0\xe5\xa5\xbd\n"

    def fake_get(url, *args, **kwargs):
        if url in {"https://subhd.tv/a/aCQ07t", "https://subhd.tv/down/aCQ07t"}:
            return _FakeResponse(status_code=200, text="<html></html>")
        if url == final_url:
            return _FakeResponse(
                status_code=200,
                content=subtitle_content,
                headers={"Content-Disposition": "attachment; filename=\"demo.srt\""},
            )
        raise AssertionError(f"unexpected url: {url}")

    def fake_post(url, *args, **kwargs):
        assert url == "https://subhd.tv/api/sub/down"
        return _FakeResponse(
            status_code=200,
            json_data={"success": True, "pass": True, "url": final_url},
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    monkeypatch.setattr(provider._session, "get", fake_get)
    monkeypatch.setattr(provider._session, "post", fake_post)

    downloaded = provider.download(candidate, query=_query())
    assert downloaded.subtitle_format == "srt"
    assert downloaded.content == subtitle_content


def test_download_subhd_captcha_required(monkeypatch):
    provider = _provider()
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

    monkeypatch.setattr(
        provider._session,
        "get",
        lambda *args, **kwargs: _FakeResponse(status_code=200, text="<html></html>"),
    )
    monkeypatch.setattr(
        provider._session,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            status_code=200,
            json_data={"success": False, "pass": False, "msg": "请验证码验证"},
            headers={"Content-Type": "application/json; charset=utf-8"},
        ),
    )

    with pytest.raises(SubtitleDownloadError):
        provider.download(candidate, query=_query())


def test_download_subhdtw_retries_mirrors(monkeypatch):
    provider = _provider()
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
    calls: list[str] = []

    def fake_download_from_domain(self, *, domain, sid, candidate, query):
        calls.append(domain)
        if len(calls) == 1:
            raise SubtitleDownloadError("subhd captcha required")
        return DownloadedSubtitle(
            content="1\n00:00:00,000 --> 00:00:01,000\n测试\n".encode("utf-8"),
            subtitle_format="srt",
            language="zh",
            filename="demo.srt",
        )

    provider._download_subhd_from_domain = types.MethodType(fake_download_from_domain, provider)
    downloaded = provider.download(candidate, query=_query())

    assert downloaded.subtitle_format == "srt"
    assert calls[0] == "subhdtw.com"
    assert len(calls) >= 2


def test_subhd_cookie_string_loaded_to_mirrors():
    provider = ChineseSubtitleProvider(
        timeout_seconds=5,
        subhd_cookie_string="cf_clearance=demo_clearance; sessionid=abc123",
    )

    for domain in SUBHD_MIRRORS:
        assert provider._session.cookies.get("cf_clearance", domain=domain) == "demo_clearance"
        assert provider._session.cookies.get("sessionid", domain=domain) == "abc123"


def test_subhd_captcha_cooldown_avoids_repeated_attempts(monkeypatch):
    provider = ChineseSubtitleProvider(timeout_seconds=5, subhd_captcha_cooldown_seconds=60)
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

    calls: list[str] = []

    def fake_download_from_domain(self, *, domain, sid, candidate, query):
        calls.append(domain)
        raise SubtitleDownloadError(f"subhd captcha required on {domain}")

    provider._download_subhd_from_domain = types.MethodType(fake_download_from_domain, provider)

    with pytest.raises(SubtitleDownloadError):
        provider.download(candidate, query=_query())

    first_attempt_calls = len(calls)
    assert first_attempt_calls == len(SUBHD_MIRRORS)

    with pytest.raises(SubtitleDownloadError):
        provider.download(candidate, query=_query())

    # second call should be blocked by cooldown before touching mirrors again
    assert len(calls) == first_attempt_calls


def test_subhd_cookiecloud_auto_sync_to_mirrors(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    def fake_post(self, url, *args, **kwargs):
        calls.append((url, kwargs.get("json")))
        return _FakeResponse(
            status_code=200,
            json_data={
                "cookie_data": {
                    "subhd.tv": [
                        {"name": "cf_clearance", "value": "demo_token", "domain": ".subhd.tv"},
                        {"name": "sessionid", "value": "abc123", "domain": ".subhd.tv"},
                    ],
                    "example.com": [
                        {"name": "sid", "value": "ignore", "domain": ".example.com"},
                    ],
                }
            },
        )

    monkeypatch.setattr("app.chinese_provider.requests.Session.post", fake_post)

    provider = ChineseSubtitleProvider(
        timeout_seconds=5,
        cookiecloud_url="https://mp.fansoar.com/cookiecloud",
        cookiecloud_key="demo_key",
        cookiecloud_password="demo_pwd",
        cookiecloud_sync_interval_seconds=0,
    )

    assert calls == [("https://mp.fansoar.com/cookiecloud/get/demo_key", {"password": "demo_pwd"})]
    for domain in SUBHD_MIRRORS:
        assert provider._session.cookies.get("cf_clearance", domain=domain) == "demo_token"
        assert provider._session.cookies.get("sessionid", domain=domain) == "abc123"


def test_detect_subhd_site_verification_page():
    html = """
    <script>
    $.ajax({type: "GET", url: "/ajax/gzh", success: function(data){ $(".btn-c").text("验证中"); }});
    </script>
    """
    assert ChineseSubtitleProvider._is_subhd_site_verification_page(html) is True
