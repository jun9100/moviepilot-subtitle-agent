from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.service import SubtitleService

from .fakes import FakeChineseProvider, make_candidate


def _build_client(tmp_path) -> TestClient:
    provider = FakeChineseProvider(
        [
            make_candidate(subtitle_id="x-1", score=111, language="zh-cn"),
            make_candidate(subtitle_id="x-2", score=90, language="zh-tw"),
        ]
    )

    settings = Settings(
        default_providers="assrt,subhd",
        default_languages="zh-cn,zh-tw",
        subtitle_output_dir=tmp_path,
        token_ttl_seconds=3600,
    )
    service = SubtitleService(settings=settings, chinese_provider=provider)

    app = create_app(settings=settings, service=service)
    return TestClient(app)


def test_health_endpoint(tmp_path):
    client = _build_client(tmp_path)

    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert "assrt" in body["providers"]


def test_standard_search_and_download(tmp_path):
    client = _build_client(tmp_path)

    search_resp = client.post(
        "/api/v1/subtitles/search",
        json={
            "title": "匹兹堡医护前线",
            "media_type": "tv",
            "season": 2,
            "episode": 5,
            "languages": ["zh-cn", "zh-tw"],
            "limit": 5,
        },
    )

    assert search_resp.status_code == 200
    items = search_resp.json()["items"]
    assert len(items) == 2

    token = items[0]["token"]
    download_resp = client.post("/api/v1/subtitles/download", json={"token": token})
    assert download_resp.status_code == 200
    assert download_resp.json()["size"] > 0


def test_moviepilot_compatible_search_and_stream_download(tmp_path):
    client = _build_client(tmp_path)

    search_resp = client.post(
        "/api/v1/moviepilot/subtitles/search",
        json={
            "title": "匹兹堡医护前线",
            "type": "series",
            "season": 2,
            "episode": 5,
            "language": "zh-cn,zh-tw",
            "imdbid": "tt31938062",
        },
    )

    assert search_resp.status_code == 200
    body = search_resp.json()
    assert body["success"] is True
    assert body["data"]["total"] == 2

    item = body["data"]["items"][0]
    download_resp = client.get(item["download_url"])
    assert download_resp.status_code == 200
    assert "测试中文字幕".encode("utf-8") in download_resp.content
    assert "attachment" in download_resp.headers.get("content-disposition", "")
