from __future__ import annotations

from app.chinese_provider import DirectSubtitleCandidate, DownloadedSubtitle


class FakeChineseProvider:
    def __init__(
        self,
        candidates: list[DirectSubtitleCandidate],
        *,
        content: bytes | None = None,
        content_by_subtitle_id: dict[str, bytes] | None = None,
        subtitle_format: str = "srt",
    ) -> None:
        self.candidates = candidates
        self.content = content or "1\n00:00:00,000 --> 00:00:01,000\n测试中文字幕\n".encode("utf-8")
        self.content_by_subtitle_id = content_by_subtitle_id or {}
        self.subtitle_format = subtitle_format
        self.search_calls = 0

    def search(self, query, *, providers):
        self.search_calls += 1
        return list(self.candidates)

    def download(self, candidate, *, query):
        content = self.content_by_subtitle_id.get(candidate.subtitle_id, self.content)
        return DownloadedSubtitle(
            content=content,
            subtitle_format=self.subtitle_format,
            language=candidate.language,
            filename=f"{candidate.subtitle_id}.{self.subtitle_format}",
        )


def make_candidate(
    *,
    subtitle_id: str,
    score: int,
    language: str = "zh-cn",
    provider: str = "assrt",
    title: str = "匹兹堡医护前线 第二季",
    release_name: str = "The Pitt S02E05",
    subtitle_format: str = "srt",
) -> DirectSubtitleCandidate:
    return DirectSubtitleCandidate(
        provider=provider,
        subtitle_id=subtitle_id,
        title=title,
        release_name=release_name,
        language=language,
        subtitle_format=subtitle_format,
        download_url=f"https://assrt.net/download/{subtitle_id}/demo.{subtitle_format}",
        page_link=f"https://assrt.net/xml/sub/{subtitle_id}.xml",
        language_tags=["zh-cn", "zh-tw"],
        matches=["episode"],
        score=score,
    )
