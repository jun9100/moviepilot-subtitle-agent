from __future__ import annotations

import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .errors import SubtitleDownloadError
from .models import SearchRequest


CHINESE_LANG_CODES = {"zh", "zh-cn", "zh-hans", "zh-tw", "zh-hant", "chs", "cht", "chi", "zho"}
SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sub"}


@dataclass
class DirectSubtitleCandidate:
    provider: str
    subtitle_id: str
    title: str
    release_name: str
    language: str
    subtitle_format: str
    download_url: str
    page_link: str | None = None
    language_tags: list[str] = field(default_factory=list)
    matches: list[str] = field(default_factory=list)
    score: int = 0


@dataclass
class DownloadedSubtitle:
    content: bytes
    subtitle_format: str
    language: str
    filename: str | None = None


class ChineseSubtitleProvider:
    """
    Chinese subtitle provider chain used to补齐 OpenSubtitles 缺失场景。

    Current strategy:
    - subhd: search + download
    - assrt: search + download
    """

    def __init__(self, *, timeout_seconds: int = 20, user_agent: str = "MoviePilotSubtitleAgent/0.2") -> None:
        self._timeout = timeout_seconds
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )

    def search(self, query: SearchRequest, *, providers: list[str]) -> list[DirectSubtitleCandidate]:
        normalized_providers = {item.strip().lower() for item in providers if item.strip()}
        keywords = self._build_keywords(query, use_subhd=("subhd" in normalized_providers))

        all_candidates: list[DirectSubtitleCandidate] = []

        if "subhd" in normalized_providers:
            for keyword in keywords:
                try:
                    all_candidates.extend(self._search_subhd(keyword))
                except Exception:
                    continue

        if "assrt" in normalized_providers:
            for keyword in keywords:
                try:
                    all_candidates.extend(self._search_assrt(keyword))
                except Exception:
                    # assrt network/SSL failures should not break the whole chain;
                    # the service will continue with other sources/fallback providers.
                    continue

        deduped = self._dedupe_candidates(all_candidates)
        filtered = [item for item in deduped if self._candidate_matches_query(item, query)]
        filtered = [item for item in filtered if self._candidate_matches_language(item, query.languages)]

        for item in filtered:
            item.score = self._score_candidate(item, query)

        filtered.sort(key=lambda item: item.score, reverse=True)
        return filtered

    def download(self, candidate: DirectSubtitleCandidate, *, query: SearchRequest) -> DownloadedSubtitle:
        if candidate.provider == "subhd":
            return self._download_subhd(candidate, query=query)

        if not candidate.download_url:
            raise SubtitleDownloadError("candidate does not include a download url")

        try:
            response = self._session.get(candidate.download_url, timeout=self._timeout, allow_redirects=True)
        except Exception as exc:
            raise SubtitleDownloadError(f"download request failed: {exc}") from exc

        if response.status_code != 200:
            raise SubtitleDownloadError(f"download request failed with status {response.status_code}")

        raw_content = response.content
        if not raw_content:
            raise SubtitleDownloadError("downloaded content is empty")

        return self._build_downloaded_subtitle(
            candidate=candidate,
            query=query,
            raw_content=raw_content,
            source_url=candidate.download_url,
            content_disposition=response.headers.get("Content-Disposition"),
        )

    def _download_subhd(self, candidate: DirectSubtitleCandidate, *, query: SearchRequest) -> DownloadedSubtitle:
        sid = (candidate.subtitle_id or "").strip()
        if not sid or sid == "unknown":
            sid = self._extract_subhd_id(candidate.page_link or candidate.download_url)
        if not sid or sid == "unknown":
            raise SubtitleDownloadError("subhd candidate does not include a valid subtitle id")

        detail_url = f"https://subhd.tv/a/{sid}"
        down_page_url = f"https://subhd.tv/down/{sid}"

        request_headers = {
            "User-Agent": self._session.headers.get("User-Agent", "MoviePilotSubtitleAgent/0.2"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": detail_url,
            "Origin": "https://subhd.tv",
        }

        try:
            # Prime cookies/token required by subhd download API.
            self._session.get(detail_url, timeout=self._timeout, allow_redirects=True)
            self._session.get(down_page_url, timeout=self._timeout, allow_redirects=True, headers=request_headers)
        except Exception as exc:
            raise SubtitleDownloadError(f"subhd preflight request failed: {exc}") from exc

        try:
            api_response = self._session.post(
                "https://subhd.tv/api/sub/down",
                json={"sid": sid, "cap": ""},
                timeout=self._timeout,
                headers={
                    **request_headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": down_page_url,
                },
            )
        except Exception as exc:
            raise SubtitleDownloadError(f"subhd download api request failed: {exc}") from exc

        if api_response.status_code != 200:
            raise SubtitleDownloadError(f"subhd download api failed with status {api_response.status_code}")

        try:
            payload = api_response.json()
        except Exception as exc:
            raise SubtitleDownloadError(f"subhd download api returned invalid json: {exc}") from exc

        if payload.get("pass") is False:
            raise SubtitleDownloadError("subhd requires captcha verification, cannot auto-download")
        if payload.get("success") is not True:
            message = str(payload.get("msg") or "subhd download api failed")
            raise SubtitleDownloadError(message)

        final_url = str(payload.get("url") or "").strip()
        if not final_url:
            raise SubtitleDownloadError("subhd download api did not return file url")

        try:
            file_response = self._session.get(
                final_url,
                timeout=self._timeout,
                allow_redirects=True,
                headers={"Referer": down_page_url},
            )
        except Exception as exc:
            raise SubtitleDownloadError(f"subhd file request failed: {exc}") from exc

        if file_response.status_code != 200:
            raise SubtitleDownloadError(f"subhd file request failed with status {file_response.status_code}")
        if not file_response.content:
            raise SubtitleDownloadError("subhd downloaded content is empty")

        return self._build_downloaded_subtitle(
            candidate=candidate,
            query=query,
            raw_content=file_response.content,
            source_url=final_url,
            content_disposition=file_response.headers.get("Content-Disposition"),
        )

    def _build_downloaded_subtitle(
        self,
        *,
        candidate: DirectSubtitleCandidate,
        query: SearchRequest,
        raw_content: bytes,
        source_url: str,
        content_disposition: str | None,
    ) -> DownloadedSubtitle:
        hinted_filename = self._extract_filename(content_disposition, source_url)
        suffix = Path(hinted_filename).suffix.lower() if hinted_filename else ""
        lower_url = source_url.lower()

        if not suffix:
            suffix = Path(urlparse(lower_url).path).suffix.lower()

        if suffix in SUBTITLE_SUFFIXES:
            format_name = suffix.lstrip(".") or candidate.subtitle_format or "srt"
            return DownloadedSubtitle(
                content=raw_content,
                subtitle_format=format_name,
                language=candidate.language,
                filename=hinted_filename,
            )

        if suffix == ".zip" or raw_content.startswith(b"PK"):
            return self._extract_from_zip(raw_content, query=query, fallback_filename=hinted_filename)

        if suffix == ".rar" or raw_content.startswith(b"Rar!"):
            return self._extract_from_rar(raw_content, query=query, fallback_filename=hinted_filename)

        # 尝试把未知类型当作字幕文本处理
        guessed_format = candidate.subtitle_format or "srt"
        return DownloadedSubtitle(
            content=raw_content,
            subtitle_format=guessed_format,
            language=candidate.language,
            filename=hinted_filename,
        )

    def _build_keywords(self, query: SearchRequest, *, use_subhd: bool) -> list[str]:
        seeds: list[str] = []
        if query.title.strip():
            seeds.append(query.title.strip())
        if query.imdb_id:
            seeds.append(query.imdb_id)
        if query.tmdb_id:
            seeds.append(str(query.tmdb_id))

        keywords = list(dict.fromkeys(seed for seed in seeds if seed))
        if not use_subhd:
            return keywords

        hints: list[str] = []
        for seed in keywords[:4]:
            try:
                hints.extend(self._search_subhd_hints(seed))
            except Exception:
                continue

        for hint in hints[:20]:
            normalized = hint.strip()
            if normalized:
                keywords.append(normalized)

        return list(dict.fromkeys(keywords))

    def _search_subhd_hints(self, keyword: str) -> list[str]:
        encoded = quote(keyword)
        url = f"https://subhd.tv/search/{encoded}"
        response = self._session.get(url, timeout=self._timeout)
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        hints: list[str] = []

        for card in soup.select("div.bg-white.shadow-sm.rounded-3.mb-4"):
            link = card.select_one("a.link-dark.align-middle[href^='/a/']")
            release_link = card.select_one("div.view-text a[href^='/a/']")

            if link:
                title = self._clean_text(link.get_text(" ", strip=True))
                if title:
                    hints.append(title)

            if release_link:
                release_name = self._clean_text(release_link.get_text(" ", strip=True))
                if release_name:
                    hints.append(release_name)

        return hints

    def _search_subhd(self, keyword: str) -> list[DirectSubtitleCandidate]:
        encoded = quote(keyword)
        url = f"https://subhd.tv/search/{encoded}"
        response = self._session.get(url, timeout=self._timeout)
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results: list[DirectSubtitleCandidate] = []

        for card in soup.select("div.bg-white.shadow-sm.rounded-3.mb-4"):
            title_anchor = card.select_one("a.link-dark.align-middle[href^='/a/']")
            if not title_anchor:
                continue

            href = title_anchor.get("href") or ""
            subtitle_id = self._extract_subhd_id(href)
            if subtitle_id == "unknown":
                continue

            title = self._clean_text(title_anchor.get_text(" ", strip=True))
            release_anchor = card.select_one("div.view-text a[href^='/a/']")
            release_name = self._clean_text(release_anchor.get_text(" ", strip=True)) if release_anchor else title

            info_tokens = [
                self._clean_text(span.get_text(" ", strip=True))
                for span in card.select("div.text-truncate.py-2.f11 span")
                if self._clean_text(span.get_text(" ", strip=True))
            ]

            language_tags = self._map_subhd_languages(info_tokens)
            language_code = self._language_code_from_tags(language_tags)
            subtitle_format = self._extract_subhd_format(info_tokens)
            page_link = urljoin(url, f"/a/{subtitle_id}")
            download_url = urljoin(url, f"/down/{subtitle_id}")

            results.append(
                DirectSubtitleCandidate(
                    provider="subhd",
                    subtitle_id=subtitle_id,
                    title=title,
                    release_name=release_name,
                    language=language_code,
                    subtitle_format=subtitle_format,
                    download_url=download_url,
                    page_link=page_link,
                    language_tags=language_tags,
                    matches=self._extract_matches(release_name),
                )
            )

        return results

    def _search_assrt(self, keyword: str) -> list[DirectSubtitleCandidate]:
        url = "https://assrt.net/sub/"
        response = self._session.get(url, params={"searchword": keyword}, timeout=self._timeout)
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results: list[DirectSubtitleCandidate] = []

        for entry in soup.select("div.subitem"):
            title_anchor = entry.select_one("a.introtitle")
            if not title_anchor:
                continue

            title = self._clean_text(title_anchor.get_text(" ", strip=True))
            page_link = urljoin(url, title_anchor.get("href") or "")
            subtitle_id = self._extract_assrt_id(title_anchor.get("href") or "")

            release_block = entry.select_one("#meta_top b")
            release_name = self._clean_text(release_block.get_text(" ", strip=True)) if release_block else title

            language_line = ""
            for span in entry.select("div#sublist_div > span"):
                text = self._clean_text(span.get_text(" ", strip=True))
                if text.startswith("语言"):
                    language_line = text
                    break

            lang_ids = [tag.get("id", "") for tag in entry.select("span.sublang-ind span[id]")]
            language_tags = self._map_assrt_languages(language_line, lang_ids)
            language_code = self._language_code_from_tags(language_tags)

            download_anchor = entry.select_one("a#downsubbtn")
            onclick = download_anchor.get("onclick", "") if download_anchor else ""
            download_path = self._extract_download_path(onclick)
            if not download_path:
                continue

            download_url = urljoin(url, download_path)
            subtitle_format = self._extract_assrt_format(entry)

            results.append(
                DirectSubtitleCandidate(
                    provider="assrt",
                    subtitle_id=subtitle_id,
                    title=title,
                    release_name=release_name,
                    language=language_code,
                    subtitle_format=subtitle_format,
                    download_url=download_url,
                    page_link=page_link,
                    language_tags=language_tags,
                    matches=self._extract_matches(release_name),
                )
            )

        return results

    @staticmethod
    def _extract_subhd_id(href: str | None) -> str:
        if not href:
            return "unknown"
        match = re.search(r"/a/([A-Za-z0-9]+)", href)
        if match:
            return match.group(1)
        stripped = href.strip("/")
        return stripped or "unknown"

    @staticmethod
    def _map_subhd_languages(tokens: list[str]) -> list[str]:
        tags: list[str] = []
        merged = " ".join(tokens).lower()

        if any(item in merged for item in ("简体", "简中", "chs", "zh-cn", "zh_hans")):
            tags.append("zh-cn")
        if any(item in merged for item in ("繁体", "繁中", "cht", "zh-tw", "zh_hant")):
            tags.append("zh-tw")
        if any(item in merged for item in ("英文", "英语", "english", "eng")):
            tags.append("en")
        if any(item in merged for item in ("双语", "bilingual", "中英")):
            tags.append("bilingual")

        if not tags:
            tags.append("unknown")
        return tags

    @staticmethod
    def _extract_subhd_format(tokens: list[str]) -> str:
        for token in tokens:
            normalized = token.strip().lower().lstrip(".")
            if normalized in {"srt", "ass", "ssa", "vtt", "sub"}:
                return normalized
        return "srt"

    @staticmethod
    def _extract_assrt_id(href: str) -> str:
        match = re.search(r"/xml/sub/\d+/(\d+)\.xml", href)
        if match:
            return match.group(1)
        return href.strip("/") or "unknown"

    @staticmethod
    def _extract_download_path(onclick: str) -> str | None:
        match = re.search(r"location\.href='([^']+)'", onclick)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _extract_assrt_format(entry: BeautifulSoup) -> str:
        format_text = ""
        for span in entry.select("div#sublist_div > span"):
            text = span.get_text(" ", strip=True)
            if text.startswith("格式"):
                format_text = text
                break

        match = re.search(r"\(([^)]+)\)", format_text)
        if match:
            return match.group(1).lower()
        if "ass" in format_text.lower():
            return "ass"
        return "srt"

    @staticmethod
    def _clean_text(value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value).strip()
        # assrt 搜索页中高亮词可能导致中文字符之间插入空格
        return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", cleaned)

    @staticmethod
    def _map_assrt_languages(language_line: str, lang_ids: list[str]) -> list[str]:
        tags: list[str] = []
        lowered = language_line.lower()

        if "简" in language_line or "chs" in lowered or any("chs" in item for item in lang_ids):
            tags.append("zh-cn")
        if "繁" in language_line or "cht" in lowered or any("cht" in item for item in lang_ids):
            tags.append("zh-tw")
        if "英" in language_line or any("eng" in item for item in lang_ids):
            tags.append("en")
        if "双语" in language_line or any("dou" in item for item in lang_ids):
            tags.append("bilingual")

        if not tags:
            tags.append("unknown")
        return tags

    @staticmethod
    def _language_code_from_tags(tags: Iterable[str]) -> str:
        normalized = set(tags)
        if "zh-cn" in normalized and "zh-tw" in normalized:
            return "zh"
        if "zh-cn" in normalized:
            return "zh-cn"
        if "zh-tw" in normalized:
            return "zh-tw"
        if "en" in normalized:
            return "en"
        return "und"

    @staticmethod
    def _extract_matches(text: str) -> list[str]:
        matches: list[str] = []
        if re.search(r"S\d{1,2}E\d{1,2}", text, re.IGNORECASE):
            matches.append("episode")
        if re.search(r"\b(1080p|2160p|720p)\b", text, re.IGNORECASE):
            matches.append("resolution")
        if re.search(r"WEB[- .]?DL|HMAX|AMZN|HBOMax", text, re.IGNORECASE):
            matches.append("source")
        return matches

    def _candidate_matches_query(self, candidate: DirectSubtitleCandidate, query: SearchRequest) -> bool:
        merged = f"{candidate.title} {candidate.release_name}"

        if query.media_type == "tv":
            season_episode = self._extract_season_episode(merged)
            season_only = self._extract_season(merged)
            if query.season and season_only and season_only != query.season:
                return False

            if query.season and query.episode:
                if season_episode:
                    season, episode = season_episode
                    if season != query.season or episode != query.episode:
                        return False
                else:
                    episode = self._extract_episode_from_text(merged)
                    episode_range = self._extract_episode_range(merged)
                    episode_upper_bound = self._extract_episode_upper_bound(merged)

                    if episode is not None and episode != query.episode:
                        return False
                    if episode_range and not (episode_range[0] <= query.episode <= episode_range[1]):
                        return False
                    if episode_upper_bound is not None and query.episode > episode_upper_bound:
                        return False

                    if (
                        episode is None
                        and not episode_range
                        and episode_upper_bound is None
                        and not self._looks_like_season_pack(candidate, query=query)
                    ):
                        # Episodic requests should avoid ambiguous single-file subtitles.
                        return False

        if query.year:
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", merged)
            if year_match and int(year_match.group(1)) != query.year:
                return False

        return True

    @staticmethod
    def _extract_season_episode(text: str) -> tuple[int, int] | None:
        match = re.search(r"S(\d{1,2})E(\d{1,2})", text, re.IGNORECASE)
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def _extract_season(text: str) -> int | None:
        season_episode = ChineseSubtitleProvider._extract_season_episode(text)
        if season_episode:
            return season_episode[0]

        match = re.search(r"\bS(\d{1,2})(?!E)\b", text, re.IGNORECASE)
        if match:
            return int(match.group(1))

        match = re.search(r"\bSeason\s*(\d{1,2})\b", text, re.IGNORECASE)
        if match:
            return int(match.group(1))

        match = re.search(r"第\s*(\d{1,2})\s*季", text)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _extract_episode_from_text(text: str) -> int | None:
        if ChineseSubtitleProvider._extract_episode_range(text):
            return None
        if ChineseSubtitleProvider._extract_episode_upper_bound(text) is not None:
            return None

        season_episode = ChineseSubtitleProvider._extract_season_episode(text)
        if season_episode:
            return season_episode[1]

        match = re.search(r"\bE(?:P)?\s*0?(\d{1,3})\b", text, re.IGNORECASE)
        if match:
            return int(match.group(1))

        match = re.search(r"第\s*0?(\d{1,3})\s*[集话]", text)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _extract_episode_range(text: str) -> tuple[int, int] | None:
        patterns = (
            r"(?i)\bE(?:P)?\s*0?(\d{1,3})\s*[-~至到]\s*E?(?:P)?\s*0?(\d{1,3})\b",
            r"第\s*0?(\d{1,3})\s*[-~至到]\s*0?(\d{1,3})\s*[集话]",
            r"(?i)\b(?:ep|e)?\s*0?(\d{1,3})\s*[-~]\s*0?(\d{1,3})\b",
        )

        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            start = int(match.group(1))
            end = int(match.group(2))
            if start > end:
                start, end = end, start
            return start, end

        return None

    @staticmethod
    def _extract_episode_upper_bound(text: str) -> int | None:
        patterns = (
            r"(?i)更新\s*[至到]\s*(?:第)?\s*(?:E(?:P)?)?\s*0?(\d{1,3})\s*(?:[集话])?",
            r"(?i)\bup\s*to\s*e?\s*0?(\d{1,3})\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _looks_like_season_pack(candidate: DirectSubtitleCandidate, *, query: SearchRequest) -> bool:
        merged = f"{candidate.title} {candidate.release_name}"
        season = ChineseSubtitleProvider._extract_season(merged)
        if query.season and season and season != query.season:
            return False

        ext = Path(urlparse(candidate.download_url).path).suffix.lower()
        if ext not in {".zip", ".rar"} and candidate.provider != "subhd":
            return False

        episode = ChineseSubtitleProvider._extract_episode_from_text(merged)
        if episode is not None and query.episode and episode == query.episode:
            return True

        episode_range = ChineseSubtitleProvider._extract_episode_range(merged)
        if episode_range and query.episode and episode_range[0] <= query.episode <= episode_range[1]:
            return True

        upper_bound = ChineseSubtitleProvider._extract_episode_upper_bound(merged)
        if upper_bound is not None and query.episode and query.episode <= upper_bound:
            return True

        if re.search(
            r"(?i)\b(complete|全集|合集|全季|全\d+集|season\s*\d+\s*complete|s\d{1,2}\s*complete)\b",
            merged,
        ):
            return True

        # Conservative fallback: require obvious title overlap to avoid cross-show mismatch.
        query_title = (query.title or "").strip().lower()
        if query_title:
            merged_norm = re.sub(r"\s+", "", merged.lower())
            title_norm = re.sub(r"\s+", "", query_title)
            if title_norm and title_norm in merged_norm:
                return True

        return False

    @staticmethod
    def _normalize_language_list(languages: list[str]) -> set[str]:
        result: set[str] = set()
        for language in languages:
            normalized = language.strip().lower()
            if normalized:
                result.add(normalized)
        return result

    def _candidate_matches_language(self, candidate: DirectSubtitleCandidate, requested_languages: list[str]) -> bool:
        requested = self._normalize_language_list(requested_languages)
        if not requested:
            requested = {"zh-cn", "zh-tw"}

        # 永远只保留中文字幕（简体/繁体）
        if candidate.language not in {"zh", "zh-cn", "zh-tw"}:
            return False

        wants_simplified = any(item in requested for item in {"zh", "zh-cn", "zh-hans", "chs", "chi", "zho"})
        wants_traditional = any(item in requested for item in {"zh", "zh-tw", "zh-hant", "cht", "chi", "zho"})

        if not wants_simplified and not wants_traditional:
            return False

        has_simplified = "zh-cn" in candidate.language_tags or candidate.language in {"zh", "zh-cn"}
        has_traditional = "zh-tw" in candidate.language_tags or candidate.language in {"zh", "zh-tw"}

        if wants_simplified and has_simplified:
            return True
        if wants_traditional and has_traditional:
            return True
        return False

    def _score_candidate(self, candidate: DirectSubtitleCandidate, query: SearchRequest) -> int:
        score = 100

        if candidate.provider == "assrt":
            score += 30
        elif candidate.provider == "subhd":
            score += 24

        if "zh-cn" in candidate.language_tags:
            score += 40
        if "zh-tw" in candidate.language_tags:
            score += 35
        if "zh-cn" in candidate.language_tags and "zh-tw" in candidate.language_tags:
            score += 20

        if "bilingual" in candidate.language_tags:
            score += 8

        ext = Path(urlparse(candidate.download_url).path).suffix.lower()
        if ext == ".zip":
            score += 25
        elif ext == ".rar":
            score -= 8
        elif ext in SUBTITLE_SUFFIXES:
            score += 30

        if candidate.subtitle_format.lower() in {"srt", "ass"}:
            score += 10

        merged = f"{candidate.title} {candidate.release_name}".lower()
        if query.title.lower() in merged:
            score += 12

        season_episode = self._extract_season_episode(merged)
        if season_episode and query.media_type == "tv":
            season, episode = season_episode
            if query.season and query.episode and season == query.season and episode == query.episode:
                score += 80
            elif query.season and season == query.season:
                score += 20
        elif query.media_type == "tv" and query.season and query.episode:
            episode = self._extract_episode_from_text(merged)
            episode_range = self._extract_episode_range(merged)
            episode_upper_bound = self._extract_episode_upper_bound(merged)
            if episode is not None:
                if episode == query.episode:
                    score += 55
                else:
                    score -= 120
            elif episode_range:
                if episode_range[0] <= query.episode <= episode_range[1]:
                    score += 45
                else:
                    score -= 130
            elif episode_upper_bound is not None:
                if query.episode <= episode_upper_bound:
                    score += 28
                else:
                    score -= 120
            elif self._looks_like_season_pack(candidate, query=query):
                score += 15
            else:
                score -= 50

        if "官方" in candidate.release_name:
            score += 6

        return score

    @staticmethod
    def _dedupe_candidates(candidates: list[DirectSubtitleCandidate]) -> list[DirectSubtitleCandidate]:
        seen: set[tuple[str, str, str]] = set()
        deduped: list[DirectSubtitleCandidate] = []
        for item in candidates:
            key = (item.provider, item.subtitle_id, item.download_url)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @staticmethod
    def _extract_filename(content_disposition: str | None, download_url: str) -> str | None:
        if content_disposition:
            # filename*=UTF-8''xxx
            star_match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
            if star_match:
                return star_match.group(1)
            match = re.search(r"filename=\"?([^\";]+)\"?", content_disposition, re.IGNORECASE)
            if match:
                return match.group(1)

        path_name = Path(urlparse(download_url).path).name
        return path_name or None

    def _extract_from_zip(self, archive_content: bytes, *, query: SearchRequest, fallback_filename: str | None) -> DownloadedSubtitle:
        with tempfile.TemporaryDirectory(prefix="subtitle_zip_") as temp_dir:
            archive_path = Path(temp_dir) / "archive.zip"
            archive_path.write_bytes(archive_content)

            extract_dir = Path(temp_dir) / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)

            try:
                with zipfile.ZipFile(archive_path, "r") as zip_file:
                    zip_file.extractall(extract_dir)
            except Exception as exc:
                raise SubtitleDownloadError(f"failed to extract zip archive: {exc}") from exc

            selected_file = self._pick_extracted_subtitle(extract_dir, query)
            if not selected_file:
                raise SubtitleDownloadError("zip archive does not contain subtitle files")

            content = selected_file.read_bytes()
            suffix = selected_file.suffix.lower().lstrip(".") or "srt"

            return DownloadedSubtitle(
                content=content,
                subtitle_format=suffix,
                language=self._guess_language_from_filename(selected_file.name),
                filename=selected_file.name or fallback_filename,
            )

    def _extract_from_rar(self, archive_content: bytes, *, query: SearchRequest, fallback_filename: str | None) -> DownloadedSubtitle:
        with tempfile.TemporaryDirectory(prefix="subtitle_rar_") as temp_dir:
            archive_path = Path(temp_dir) / "archive.rar"
            archive_path.write_bytes(archive_content)

            extract_dir = Path(temp_dir) / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)

            proc = subprocess.run(
                ["bsdtar", "-xf", str(archive_path), "-C", str(extract_dir)],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise SubtitleDownloadError(f"failed to extract rar archive: {proc.stderr.strip() or proc.stdout.strip()}")

            selected_file = self._pick_extracted_subtitle(extract_dir, query)
            if not selected_file:
                raise SubtitleDownloadError("rar archive does not contain subtitle files")

            content = selected_file.read_bytes()
            suffix = selected_file.suffix.lower().lstrip(".") or "srt"

            return DownloadedSubtitle(
                content=content,
                subtitle_format=suffix,
                language=self._guess_language_from_filename(selected_file.name),
                filename=selected_file.name or fallback_filename,
            )

    def _pick_extracted_subtitle(self, root: Path, query: SearchRequest) -> Path | None:
        files = [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in SUBTITLE_SUFFIXES
        ]
        if not files:
            return None

        requested = self._normalize_language_list(query.languages)
        wants_simplified = any(item in requested for item in {"zh", "zh-cn", "zh-hans", "chs", "chi", "zho"})
        wants_traditional = any(item in requested for item in {"zh", "zh-tw", "zh-hant", "cht", "chi", "zho"})
        wants_episode = query.media_type == "tv" and query.season and query.episode
        title_tokens = [
            token.lower()
            for token in re.findall(r"[\w\u4e00-\u9fff]+", query.title or "")
            if len(token) >= 2
        ]

        def file_score(path: Path) -> int:
            name = path.name.lower()
            score = 0

            if path.suffix.lower() == ".srt":
                score += 20
            elif path.suffix.lower() == ".ass":
                score += 10

            if any(token in name for token in ["chs", "sc", "zh-cn", "简", "gb"]):
                score += 50 if wants_simplified else 10
            if any(token in name for token in ["cht", "tc", "zh-tw", "繁", "big5"]):
                score += 45 if wants_traditional else 10
            if any(token in name for token in ["eng", "english", "英文"]):
                score -= 20
            if "bilingual" in name or "双语" in name:
                score += 8

            if wants_episode:
                season_episode = self._extract_season_episode(name)
                season_only = self._extract_season(name)
                episode_only = self._extract_episode_from_text(name)
                episode_range = self._extract_episode_range(name)
                episode_upper_bound = self._extract_episode_upper_bound(name)

                if season_episode:
                    if season_episode[0] == query.season and season_episode[1] == query.episode:
                        score += 220
                    elif season_episode[0] == query.season:
                        score -= 120
                    else:
                        score -= 180
                else:
                    if season_only and season_only != query.season:
                        score -= 120
                    if episode_only is not None:
                        if episode_only == query.episode:
                            score += 180
                        else:
                            score -= 140
                    elif episode_range:
                        if episode_range[0] <= query.episode <= episode_range[1]:
                            score += 130
                        else:
                            score -= 140
                    elif episode_upper_bound is not None:
                        if query.episode <= episode_upper_bound:
                            score += 80
                        else:
                            score -= 120
                    elif re.search(r"(?i)\b(complete|全集|全\d+集)\b", name):
                        score += 25
                    else:
                        score -= 20

            if title_tokens:
                hit_count = sum(1 for token in title_tokens if token in name)
                score += hit_count * 8

            return score

        files.sort(key=file_score, reverse=True)
        return files[0]

    @staticmethod
    def _guess_language_from_filename(filename: str) -> str:
        lowered = filename.lower()
        has_simplified = any(token in lowered for token in ["chs", "zh-cn", "简"])
        has_traditional = any(token in lowered for token in ["cht", "zh-tw", "繁"])

        if has_simplified and has_traditional:
            return "zh"
        if has_simplified:
            return "zh-cn"
        if has_traditional:
            return "zh-tw"
        return "zh"
