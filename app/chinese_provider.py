from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlparse
from uuid import uuid4

import requests
from bs4 import BeautifulSoup

from .errors import SubtitleCaptchaError, SubtitleDownloadError, SubtitleNotFoundError
from .models import SearchRequest


CHINESE_LANG_CODES = {"zh", "zh-cn", "zh-hans", "zh-tw", "zh-hant", "chs", "cht", "chi", "zho"}
SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
SUBHD_MIRRORS = ("subhd.tv", "subhdtw.com", "subhd.cc", "subhd.me")
TITLE_NOISE_TOKENS = {
    "1080p",
    "2160p",
    "720p",
    "webrip",
    "web",
    "webdl",
    "bluray",
    "bdrip",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "aac",
    "ddp",
    "hdr",
    "dv",
    "srt",
    "ass",
    "ssa",
    "vtt",
    "mkv",
    "mp4",
}
logger = logging.getLogger(__name__)


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


@dataclass
class SubhdCaptchaChallenge:
    challenge_id: str
    provider: str
    subtitle_id: str
    domain: str
    detail_url: str
    down_page_url: str
    image_content: bytes
    image_content_type: str
    image_path: str
    candidate: DirectSubtitleCandidate
    query: SearchRequest
    created_at_monotonic: float


class ChineseSubtitleProvider:
    """
    Chinese subtitle provider chain used to补齐 OpenSubtitles 缺失场景。

    Current strategy:
    - subhd/subhdtw: search + download (with mirror retries)
    - assrt: search + download
    """

    def __init__(
        self,
        *,
        timeout_seconds: int = 20,
        user_agent: str = "MoviePilotSubtitleAgent/0.2",
        allow_season_pack_for_episode: bool = True,
        strict_media_type_filter: bool = True,
        subhd_captcha_cooldown_seconds: int = 1800,
        subhd_cookie_string: str | None = None,
        subhd_cookie_file: str | None = None,
        cookiecloud_url: str | None = None,
        cookiecloud_key: str | None = None,
        cookiecloud_password: str | None = None,
        cookiecloud_sync_interval_seconds: int = 1800,
    ) -> None:
        self._timeout = timeout_seconds
        self._allow_season_pack_for_episode = allow_season_pack_for_episode
        self._strict_media_type_filter = strict_media_type_filter
        self._subhd_captcha_cooldown_seconds = max(0, int(subhd_captcha_cooldown_seconds or 0))
        self._subhd_domain_cooldown_until: dict[str, float] = {}
        self._cookiecloud_url = str(cookiecloud_url or "").strip().rstrip("/")
        self._cookiecloud_key = str(cookiecloud_key or "").strip()
        self._cookiecloud_password = str(cookiecloud_password or "").strip()
        self._cookiecloud_sync_interval_seconds = max(0, int(cookiecloud_sync_interval_seconds or 0))
        self._cookiecloud_last_sync_at = 0.0
        self._captcha_challenge_ttl_seconds = max(300, self._subhd_captcha_cooldown_seconds or 1800)
        self._captcha_lock = threading.RLock()
        self._captcha_challenges: dict[str, SubhdCaptchaChallenge] = {}
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        self._sync_subhd_cookies_from_cookiecloud(force=True)
        self._apply_subhd_cookies(cookie_string=subhd_cookie_string, cookie_file=subhd_cookie_file)

    def search(self, query: SearchRequest, *, providers: list[str]) -> list[DirectSubtitleCandidate]:
        normalized_providers = {item.strip().lower() for item in providers if item.strip()}
        use_subhd_hints = bool({"subhd", "subhdtw"} & normalized_providers)
        keywords = self._build_keywords(query, use_subhd=use_subhd_hints)

        all_candidates: list[DirectSubtitleCandidate] = []

        if "subhd" in normalized_providers:
            for keyword in keywords:
                try:
                    all_candidates.extend(self._search_subhd(keyword))
                except Exception:
                    continue

        if "subhdtw" in normalized_providers:
            for keyword in keywords:
                try:
                    all_candidates.extend(self._search_subhdtw(keyword))
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
        if candidate.provider in {"subhd", "subhdtw"}:
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

    def get_captcha_image(self, challenge_id: str) -> tuple[bytes, str]:
        challenge = self._get_captcha_challenge(challenge_id)
        if not challenge.image_content:
            raise SubtitleNotFoundError("captcha image unavailable, please refresh challenge")
        return challenge.image_content, challenge.image_content_type

    def solve_captcha(self, challenge_id: str, *, code: str) -> tuple[DownloadedSubtitle, DirectSubtitleCandidate, SearchRequest]:
        challenge = self._get_captcha_challenge(challenge_id)
        captcha_code = str(code or "").strip()
        if not captcha_code:
            raise SubtitleCaptchaError("captcha code is required", data=self._captcha_error_data(challenge))

        # Re-prime subhd detail/down pages before manual submit. Without this, subhd may
        # return "temporary page expired" even for fresh user replies.
        try:
            self._prime_subhd_download_context(
                domain=challenge.domain,
                detail_url=challenge.detail_url,
                down_page_url=challenge.down_page_url,
            )
        except Exception:
            pass

        payload = self._request_subhd_download_payload(
            domain=challenge.domain,
            sid=challenge.subtitle_id,
            detail_url=challenge.detail_url,
            down_page_url=challenge.down_page_url,
            captcha_code=captcha_code,
        )

        message = str(payload.get("msg") or "")
        if (payload.get("pass") is False or payload.get("success") is not True) and self._is_subhd_temporary_page_expired(
            message
        ):
            # Some mirrors return a stale-page error on first submit. Retry once with
            # a fresh down-page preflight and the same captcha text.
            try:
                self._prime_subhd_download_context(
                    domain=challenge.domain,
                    detail_url=challenge.detail_url,
                    down_page_url=challenge.down_page_url,
                )
                payload = self._request_subhd_download_payload(
                    domain=challenge.domain,
                    sid=challenge.subtitle_id,
                    detail_url=challenge.detail_url,
                    down_page_url=challenge.down_page_url,
                    captcha_code=captcha_code,
                )
            except Exception:
                pass

        if payload.get("pass") is False or payload.get("success") is not True:
            refreshed_payload: dict[str, Any] | None = None
            # SubHD may return "page expired" on wrong/late code without SVG challenge body.
            # Proactively request a fresh challenge so follow-up attempts still have an image.
            try:
                refreshed_payload = self._request_subhd_download_payload(
                    domain=challenge.domain,
                    sid=challenge.subtitle_id,
                    detail_url=challenge.detail_url,
                    down_page_url=challenge.down_page_url,
                    captcha_code="",
                )
            except Exception:
                refreshed_payload = None
            refreshed = self._refresh_captcha_challenge(
                challenge,
                captcha_payload=refreshed_payload or payload,
            )
            message = self._normalize_subhd_captcha_message(payload.get("msg"))
            raise SubtitleCaptchaError(message, data=self._captcha_error_data(refreshed))

        downloaded = self._download_subhd_file_from_payload(
            payload=payload,
            domain=challenge.domain,
            down_page_url=challenge.down_page_url,
            candidate=challenge.candidate,
            query=challenge.query,
        )

        self._remove_captcha_challenge(challenge.challenge_id)
        return downloaded, challenge.candidate, challenge.query

    def _download_subhd(self, candidate: DirectSubtitleCandidate, *, query: SearchRequest) -> DownloadedSubtitle:
        self._sync_subhd_cookies_from_cookiecloud(force=False)
        sid = (candidate.subtitle_id or "").strip()
        if not sid or sid == "unknown":
            sid = self._extract_subhd_id(candidate.page_link or candidate.download_url)
        if not sid or sid == "unknown":
            raise SubtitleDownloadError("subhd candidate does not include a valid subtitle id")
        domains = [domain for domain in self._subhd_domain_order(candidate) if not self._is_subhd_domain_in_cooldown(domain)]
        if not domains:
            raise SubtitleDownloadError(
                "subhd mirrors in verification cooldown (/ajax/gzh or captcha), cannot auto-download now"
            )

        last_error: Exception | None = None
        captcha_encountered = False
        captcha_error: SubtitleCaptchaError | None = None

        for _ in range(2):
            attempted = False
            for domain in domains:
                if self._is_subhd_domain_in_cooldown(domain):
                    continue
                attempted = True
                try:
                    return self._download_subhd_from_domain(
                        domain=domain,
                        sid=sid,
                        candidate=candidate,
                        query=query,
                    )
                except Exception as exc:
                    last_error = exc
                    if "captcha" in str(exc).lower():
                        captcha_encountered = True
                        if isinstance(exc, SubtitleCaptchaError) and captcha_error is None:
                            captcha_error = exc
                        self._mark_subhd_domain_cooldown(domain)
                    continue
            if not attempted:
                break

        if captcha_encountered:
            if self._sync_subhd_cookies_from_cookiecloud(force=True):
                self._subhd_domain_cooldown_until.clear()
                refreshed_domains = self._subhd_domain_order(candidate)
                for domain in refreshed_domains:
                    try:
                        return self._download_subhd_from_domain(
                            domain=domain,
                            sid=sid,
                            candidate=candidate,
                            query=query,
                        )
                    except Exception as exc:
                        last_error = exc
                        if isinstance(exc, SubtitleCaptchaError) and captcha_error is None:
                            captcha_error = exc
            if captcha_error is not None:
                raise SubtitleCaptchaError(
                    "subhd requires letter captcha verification before download",
                    data=captcha_error.data,
                ) from captcha_error
            raise SubtitleDownloadError("subhd requires letter captcha verification before download")
        if last_error:
            raise SubtitleDownloadError(f"subhd download failed on all mirrors: {last_error}") from last_error
        raise SubtitleDownloadError("subhd download failed on all mirrors")

    def _download_subhd_from_domain(
        self,
        *,
        domain: str,
        sid: str,
        candidate: DirectSubtitleCandidate,
        query: SearchRequest,
    ) -> DownloadedSubtitle:
        base_url = f"https://{domain}"
        detail_url = f"{base_url}/a/{sid}"
        down_page_url = f"{base_url}/down/{sid}"

        down_html = self._prime_subhd_download_context(
            domain=domain,
            detail_url=detail_url,
            down_page_url=down_page_url,
        )

        payload = self._request_subhd_download_payload(
            domain=domain,
            sid=sid,
            detail_url=detail_url,
            down_page_url=down_page_url,
            captcha_code="",
        )

        if payload.get("pass") is False:
            challenge = self._create_captcha_challenge(
                domain=domain,
                sid=sid,
                candidate=candidate,
                query=query,
                detail_url=detail_url,
                down_page_url=down_page_url,
                html=down_html,
                captcha_payload=payload,
            )
            raise SubtitleCaptchaError(
                f"subhd letter captcha required on {domain}",
                data=self._captcha_error_data(challenge),
            )
        if payload.get("success") is not True:
            message = str(payload.get("msg") or "subhd download api failed")
            if self._message_mentions_captcha(message):
                challenge = self._create_captcha_challenge(
                    domain=domain,
                    sid=sid,
                    candidate=candidate,
                    query=query,
                    detail_url=detail_url,
                    down_page_url=down_page_url,
                    html=down_html,
                    captcha_payload=payload,
                )
                raise SubtitleCaptchaError(message, data=self._captcha_error_data(challenge))
            raise SubtitleDownloadError(message)

        return self._download_subhd_file_from_payload(
            payload=payload,
            domain=domain,
            down_page_url=down_page_url,
            candidate=candidate,
            query=query,
        )

    def _request_subhd_download_payload(
        self,
        *,
        domain: str,
        sid: str,
        detail_url: str,
        down_page_url: str,
        captcha_code: str,
    ) -> dict[str, Any]:
        base_url = f"https://{domain}"
        request_headers = {
            "User-Agent": self._session.headers.get("User-Agent", "MoviePilotSubtitleAgent/0.2"),
            "Accept": "application/json, text/plain, */*",
            "Referer": down_page_url,
            "Origin": base_url,
            "Content-Type": "application/json",
        }
        try:
            api_response = self._session.post(
                f"{base_url}/api/sub/down",
                json={"sid": sid, "cap": captcha_code},
                timeout=self._timeout,
                headers={
                    **request_headers,
                    "Referer": down_page_url,
                },
            )
        except Exception as exc:
            raise SubtitleDownloadError(f"subhd download api request failed ({domain}): {exc}") from exc

        if api_response.status_code != 200:
            raise SubtitleDownloadError(f"subhd download api failed ({domain}) with status {api_response.status_code}")

        try:
            payload = api_response.json()
        except Exception as exc:
            raise SubtitleDownloadError(f"subhd download api returned invalid json ({domain}): {exc}") from exc
        return payload

    def _download_subhd_file_from_payload(
        self,
        *,
        payload: dict[str, Any],
        domain: str,
        down_page_url: str,
        candidate: DirectSubtitleCandidate,
        query: SearchRequest,
    ) -> DownloadedSubtitle:
        base_url = f"https://{domain}"
        final_url = str(payload.get("url") or "").strip()
        if not final_url:
            raise SubtitleDownloadError("subhd download api did not return file url")
        if final_url.startswith("/"):
            final_url = urljoin(base_url, final_url)

        try:
            file_response = self._session.get(
                final_url,
                timeout=self._timeout,
                allow_redirects=True,
                headers={"Referer": down_page_url},
            )
        except Exception as exc:
            raise SubtitleDownloadError(f"subhd file request failed ({domain}): {exc}") from exc

        if file_response.status_code != 200:
            raise SubtitleDownloadError(f"subhd file request failed ({domain}) with status {file_response.status_code}")
        if not file_response.content:
            raise SubtitleDownloadError("subhd downloaded content is empty")

        return self._build_downloaded_subtitle(
            candidate=candidate,
            query=query,
            raw_content=file_response.content,
            source_url=final_url,
            content_disposition=file_response.headers.get("Content-Disposition"),
        )

    def _prime_subhd_download_context(self, *, domain: str, detail_url: str, down_page_url: str) -> str:
        base_url = f"https://{domain}"
        request_headers = {
            "User-Agent": self._session.headers.get("User-Agent", "MoviePilotSubtitleAgent/0.2"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": detail_url,
            "Origin": base_url,
        }
        try:
            # Prime cookies/token required by subhd download API.
            self._session.get(detail_url, timeout=self._timeout, allow_redirects=True)
            down_response = self._session.get(
                down_page_url,
                timeout=self._timeout,
                allow_redirects=True,
                headers=request_headers,
            )
        except Exception as exc:
            raise SubtitleDownloadError(f"subhd preflight request failed ({domain}): {exc}") from exc

        # Cloudflare anti-bot preflight link.
        try:
            down_soup = BeautifulSoup(down_response.text, "html.parser")
            cf_link = down_soup.select_one("a[href*='/cdn-cgi/content?id=']")
            if cf_link:
                href = str(cf_link.get("href") or "").strip()
                if href:
                    content_url = href if href.startswith("http") else urljoin(base_url, href)
                    self._session.get(
                        content_url,
                        timeout=self._timeout,
                        allow_redirects=True,
                        headers={**request_headers, "Referer": down_page_url},
                    )
        except Exception:
            pass
        return str(getattr(down_response, "text", "") or "")

    def _subhd_domain_order(self, candidate: DirectSubtitleCandidate) -> list[str]:
        domains = list(SUBHD_MIRRORS)

        preferred = "subhdtw.com" if candidate.provider == "subhdtw" else "subhd.tv"
        if preferred in domains:
            domains.remove(preferred)
            domains.insert(0, preferred)

        for raw in (candidate.page_link or "", candidate.download_url or ""):
            try:
                host = (urlparse(raw).hostname or "").lower()
            except Exception:
                host = ""
            if host in domains:
                domains.remove(host)
                domains.insert(0, host)
                break

        return domains

    def _apply_subhd_cookies(self, *, cookie_string: str | None, cookie_file: str | None) -> None:
        loaded = 0

        raw_cookie = str(cookie_string or "").strip()
        if raw_cookie:
            pairs = [item.strip() for item in raw_cookie.split(";") if "=" in item]
            for pair in pairs:
                name, value = pair.split("=", 1)
                name = name.strip()
                value = value.strip()
                if not name:
                    continue
                for domain in SUBHD_MIRRORS:
                    self._session.cookies.set(name, value, domain=domain, path="/")
                    loaded += 1

        path_text = str(cookie_file or "").strip()
        if path_text:
            cookie_path = Path(path_text)
            if cookie_path.is_file():
                try:
                    jar = MozillaCookieJar(str(cookie_path))
                    jar.load(ignore_discard=True, ignore_expires=True)
                    for cookie in jar:
                        domain = (cookie.domain or "").lstrip(".").lower()
                        if domain in SUBHD_MIRRORS:
                            self._session.cookies.set_cookie(cookie)
                            loaded += 1
                except Exception as exc:
                    logger.warning("failed to load subhd cookie file %s: %s", cookie_path, exc)

        if loaded:
            logger.info("loaded %d subhd cookie entries", loaded)

    def _sync_subhd_cookies_from_cookiecloud(self, *, force: bool) -> bool:
        if not (self._cookiecloud_url and self._cookiecloud_key and self._cookiecloud_password):
            return False

        now = time.monotonic()
        interval = self._cookiecloud_sync_interval_seconds
        if not force and interval > 0 and (now - self._cookiecloud_last_sync_at) < interval:
            return False

        self._cookiecloud_last_sync_at = now
        cookie_string = self._fetch_subhd_cookie_string_from_cookiecloud()
        if not cookie_string:
            return False

        self._apply_subhd_cookies(cookie_string=cookie_string, cookie_file=None)
        logger.info("synced subhd cookies from cookiecloud")
        return True

    def _fetch_subhd_cookie_string_from_cookiecloud(self) -> str:
        endpoint = urljoin(f"{self._cookiecloud_url}/", f"get/{self._cookiecloud_key}")
        try:
            response = self._session.post(
                endpoint,
                json={"password": self._cookiecloud_password},
                timeout=self._timeout,
                allow_redirects=True,
            )
        except Exception as exc:
            logger.warning("cookiecloud request failed: %s", exc)
            return ""

        if response.status_code != 200:
            logger.warning("cookiecloud request returned status %s", response.status_code)
            return ""

        try:
            payload: Any = response.json()
        except Exception as exc:
            logger.warning("cookiecloud response decode failed: %s", exc)
            return ""

        cookie_data: Any
        if isinstance(payload, dict) and "cookie_data" in payload and isinstance(payload.get("cookie_data"), dict):
            cookie_data = payload.get("cookie_data")
        else:
            cookie_data = payload

        if not isinstance(cookie_data, dict):
            logger.warning("cookiecloud payload format is not supported")
            return ""

        merged: dict[str, str] = {}
        for domain, raw_item in cookie_data.items():
            domain_text = str(domain or "").lstrip(".").lower()
            if not self._is_subhd_domain(domain_text):
                continue

            if isinstance(raw_item, str):
                for part in [item.strip() for item in raw_item.split(";") if "=" in item]:
                    name, value = part.split("=", 1)
                    name = name.strip()
                    value = value.strip()
                    if name:
                        merged[name] = value
                continue

            if not isinstance(raw_item, list):
                continue

            for cookie in raw_item:
                if not isinstance(cookie, dict):
                    continue
                name = str(cookie.get("name") or "").strip()
                value = str(cookie.get("value") or "").strip()
                cookie_domain = str(cookie.get("domain") or "").lstrip(".").lower()
                if not name:
                    continue
                if cookie_domain and not self._is_subhd_domain(cookie_domain):
                    continue
                merged[name] = value

        return "; ".join(f"{name}={value}" for name, value in merged.items())

    @staticmethod
    def _is_subhd_domain(domain: str) -> bool:
        text = str(domain or "").lstrip(".").lower()
        if not text:
            return False
        return any(text == mirror or text.endswith(f".{mirror}") for mirror in SUBHD_MIRRORS)

    def _mark_subhd_domain_cooldown(self, domain: str) -> None:
        if self._subhd_captcha_cooldown_seconds <= 0:
            return
        self._subhd_domain_cooldown_until[domain] = time.monotonic() + self._subhd_captcha_cooldown_seconds

    def _is_subhd_domain_in_cooldown(self, domain: str) -> bool:
        if self._subhd_captcha_cooldown_seconds <= 0:
            return False
        until = self._subhd_domain_cooldown_until.get(domain)
        if not until:
            return False
        if until <= time.monotonic():
            self._subhd_domain_cooldown_until.pop(domain, None)
            return False
        return True

    def _create_captcha_challenge(
        self,
        *,
        domain: str,
        sid: str,
        candidate: DirectSubtitleCandidate,
        query: SearchRequest,
        detail_url: str,
        down_page_url: str,
        html: str,
        captcha_payload: dict[str, Any] | None = None,
    ) -> SubhdCaptchaChallenge:
        self._cleanup_captcha_challenges()

        image_content, image_content_type, image_path = self._extract_subhd_captcha_image(
            html=html,
            base_url=f"https://{domain}",
            down_page_url=down_page_url,
            captcha_payload=captcha_payload,
        )

        challenge = SubhdCaptchaChallenge(
            challenge_id=uuid4().hex,
            provider=candidate.provider,
            subtitle_id=sid,
            domain=domain,
            detail_url=detail_url,
            down_page_url=down_page_url,
            image_content=image_content,
            image_content_type=image_content_type or "image/png",
            image_path=image_path,
            candidate=candidate,
            query=query,
            created_at_monotonic=time.monotonic(),
        )
        with self._captcha_lock:
            self._captcha_challenges[challenge.challenge_id] = challenge
        return challenge

    def _refresh_captcha_challenge(
        self,
        challenge: SubhdCaptchaChallenge,
        *,
        captcha_payload: dict[str, Any] | None = None,
    ) -> SubhdCaptchaChallenge:
        def _fetch_down_page_html() -> str:
            try:
                response = self._session.get(
                    challenge.down_page_url,
                    timeout=self._timeout,
                    allow_redirects=True,
                    headers={
                        "Referer": challenge.detail_url,
                        "User-Agent": self._session.headers.get("User-Agent", "MoviePilotSubtitleAgent/0.2"),
                    },
                )
                return response.text if response.status_code == 200 else ""
            except Exception:
                return ""

        refreshed = self._create_captcha_challenge(
            domain=challenge.domain,
            sid=challenge.subtitle_id,
            candidate=challenge.candidate,
            query=challenge.query,
            detail_url=challenge.detail_url,
            down_page_url=challenge.down_page_url,
            html="",
            captcha_payload=captcha_payload,
        )

        # Some subhd refresh payloads contain neither SVG nor usable image URL.
        # Retry from down page HTML to avoid generating an empty captcha image endpoint.
        if not refreshed.image_content:
            fallback_html = _fetch_down_page_html()
            if fallback_html:
                fallback = self._create_captcha_challenge(
                    domain=challenge.domain,
                    sid=challenge.subtitle_id,
                    candidate=challenge.candidate,
                    query=challenge.query,
                    detail_url=challenge.detail_url,
                    down_page_url=challenge.down_page_url,
                    html=fallback_html,
                    captcha_payload=None,
                )
                self._remove_captcha_challenge(refreshed.challenge_id)
                refreshed = fallback

        # Last-resort fallback: keep previous image instead of returning 200 with empty body.
        if not refreshed.image_content and challenge.image_content:
            refreshed.image_content = challenge.image_content
            refreshed.image_content_type = challenge.image_content_type
            refreshed.image_path = challenge.image_path

        self._remove_captcha_challenge(challenge.challenge_id)
        return refreshed

    def _get_captcha_challenge(self, challenge_id: str) -> SubhdCaptchaChallenge:
        self._cleanup_captcha_challenges()
        key = str(challenge_id or "").strip()
        with self._captcha_lock:
            challenge = self._captcha_challenges.get(key)
        if challenge is None:
            raise SubtitleNotFoundError("captcha challenge not found or expired")
        return challenge

    def _remove_captcha_challenge(self, challenge_id: str) -> None:
        key = str(challenge_id or "").strip()
        if not key:
            return
        with self._captcha_lock:
            self._captcha_challenges.pop(key, None)

    def _cleanup_captcha_challenges(self) -> None:
        now = time.monotonic()
        expired: list[str] = []
        with self._captcha_lock:
            for challenge_id, challenge in self._captcha_challenges.items():
                if (now - challenge.created_at_monotonic) >= self._captcha_challenge_ttl_seconds:
                    expired.append(challenge_id)
            for challenge_id in expired:
                self._captcha_challenges.pop(challenge_id, None)

    def _captcha_error_data(self, challenge: SubhdCaptchaChallenge) -> dict[str, Any]:
        image_available = bool(challenge.image_content)
        image_path = f"/api/v1/subtitles/captcha/image/{challenge.challenge_id}" if image_available else ""
        return {
            "captcha": {
                "challenge_id": challenge.challenge_id,
                "provider": challenge.provider,
                "subtitle_id": challenge.subtitle_id,
                "domain": challenge.domain,
                "detail_url": challenge.detail_url,
                "image_path": image_path,
                "image_available": image_available,
            }
        }

    def _extract_subhd_captcha_image(
        self,
        *,
        html: str,
        base_url: str,
        down_page_url: str,
        captcha_payload: dict[str, Any] | None,
    ) -> tuple[bytes, str, str]:
        image_url = self._extract_subhd_captcha_image_url(html, base_url=base_url)
        if image_url:
            try:
                response = self._session.get(
                    image_url,
                    timeout=self._timeout,
                    allow_redirects=True,
                    headers={
                        "Referer": down_page_url,
                        "User-Agent": self._session.headers.get("User-Agent", "MoviePilotSubtitleAgent/0.2"),
                    },
                )
                if response.status_code == 200 and response.content:
                    return (
                        response.content,
                        str(response.headers.get("Content-Type") or "image/png").split(";", 1)[0],
                        urlparse(image_url).path or "",
                    )
            except Exception:
                pass

        image_content, image_content_type, image_path = self._extract_subhd_captcha_svg(captcha_payload)
        if image_content:
            return image_content, image_content_type, image_path
        return b"", "image/png", ""

    @staticmethod
    def _extract_subhd_captcha_svg(captcha_payload: dict[str, Any] | None) -> tuple[bytes, str, str]:
        if not isinstance(captcha_payload, dict):
            return b"", "image/png", ""
        message = captcha_payload.get("msg")
        if not isinstance(message, str):
            return b"", "image/png", ""
        text = message.strip()
        if not text:
            return b"", "image/png", ""
        if "<svg" not in text.lower() or "</svg>" not in text.lower():
            return b"", "image/png", ""
        return text.encode("utf-8"), "image/svg+xml", "subhd-captcha.svg"

    @staticmethod
    def _extract_subhd_captcha_image_url(html: str, *, base_url: str) -> str:
        soup = BeautifulSoup(str(html or ""), "html.parser")

        prioritized_sources: list[str] = []
        input_node = soup.find("input", attrs={"id": "gzhcode"})
        if input_node is not None:
            for node in input_node.find_all_previous("img", limit=5):
                src = str(node.get("src") or "").strip()
                if src:
                    prioritized_sources.append(src)
            parent = input_node.parent
            if parent is not None:
                for node in parent.find_all("img"):
                    src = str(node.get("src") or "").strip()
                    if src:
                        prioritized_sources.append(src)

        for selector in (
            "img[src*='captcha']",
            "img[src*='code']",
            "img[src*='verify']",
            "img[src*='yzm']",
            "img[src*='gzh']",
        ):
            for node in soup.select(selector):
                src = str(node.get("src") or "").strip()
                if src:
                    prioritized_sources.append(src)

        for raw_src in prioritized_sources:
            lowered = raw_src.lower()
            if "poster" in lowered or "logo" in lowered or "favicon" in lowered:
                continue
            return raw_src if raw_src.startswith("http") else urljoin(base_url, raw_src)

        return ""

    @staticmethod
    def _message_mentions_captcha(message: str) -> bool:
        lowered = str(message or "").strip().lower()
        if not lowered:
            return False
        return (
            "captcha" in lowered
            or "验证码" in lowered
            or "驗證碼" in lowered
            or "验证" in lowered
            or "驗證" in lowered
            or "/ajax/gzh" in lowered
        )

    @staticmethod
    def _is_subhd_temporary_page_expired(message: Any) -> bool:
        text = str(message or "").strip()
        if not text:
            return False
        lowered = text.lower()
        markers = (
            "临时页面已经失效",
            "臨時頁面已經失效",
            "页面已经失效",
            "頁面已經失效",
            "时间过长",
            "時間過長",
            "page expired",
            "temporary page",
            "captcha expired",
        )
        return any(marker in text or marker in lowered for marker in markers)

    @staticmethod
    def _normalize_subhd_captcha_message(message: Any) -> str:
        text = str(message or "").strip()
        if not text:
            return "subhd captcha validation failed"
        lowered = text.lower()
        if "<svg" in lowered and "</svg>" in lowered:
            return "subhd captcha validation failed"
        if ChineseSubtitleProvider._is_subhd_temporary_page_expired(text):
            return "subhd captcha expired or invalid, please retry with latest challenge"
        return text

    @staticmethod
    def _looks_like_captcha_page(html: str) -> bool:
        text = str(html or "").lower()
        return (
            "captcha" in text
            or "验证码" in text
            or "驗證碼" in text
            or "提交验证" in text
            or "提交驗證" in text
            or "gzhcode" in text
            or "g-recaptcha" in text
            or "/cdn-cgi/challenge" in text
        )

    @staticmethod
    def _is_subhd_site_verification_page(html: str) -> bool:
        text = str(html or "").lower()
        return "/ajax/gzh" in text or "验证获取下载地址" in text or "验证中" in text

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
        return self._search_subhd_site(keyword, base_url="https://subhd.tv", provider_name="subhd")

    def _search_subhdtw(self, keyword: str) -> list[DirectSubtitleCandidate]:
        return self._search_subhd_site(keyword, base_url="https://subhdtw.com", provider_name="subhdtw")

    def _search_subhd_site(self, keyword: str, *, base_url: str, provider_name: str) -> list[DirectSubtitleCandidate]:
        encoded = quote(keyword)
        url = f"{base_url}/search/{encoded}"
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
                    provider=provider_name,
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
            episode = self._extract_episode_from_text(merged)
            episode_range = self._extract_episode_range(merged)
            episode_upper_bound = self._extract_episode_upper_bound(merged)
            season_pack = self._looks_like_season_pack(candidate, query=query)
            title_overlap = self._title_overlap_score(query.title, merged)
            chinese_overlap = self._chinese_overlap_score(query.title, merged)

            has_tv_markers = bool(
                season_episode
                or season_only is not None
                or episode is not None
                or episode_range
                or episode_upper_bound is not None
                or season_pack
            )
            if self._strict_media_type_filter and query.season and query.episode and not has_tv_markers:
                # Strict tv-mode should not accept movie-like candidates with no season/episode signals.
                return False

            if self._strict_media_type_filter:
                has_strong_episode_signal = False
                if season_episode:
                    has_strong_episode_signal = (
                        (not query.season or season_episode[0] == query.season)
                        and (not query.episode or season_episode[1] == query.episode)
                    )
                elif query.episode:
                    if episode is not None and episode == query.episode:
                        has_strong_episode_signal = True
                    elif episode_range and episode_range[0] <= query.episode <= episode_range[1]:
                        has_strong_episode_signal = True
                    elif episode_upper_bound is not None and query.episode <= episode_upper_bound:
                        has_strong_episode_signal = True

                # If both sides have Chinese words but no overlap, it's very likely another show.
                # Skip this rule for Japanese-title queries (kana + kanji mix).
                if chinese_overlap == 0.0 and not self._contains_japanese_kana(query.title):
                    return False

                # For CJK queries, reject weak season-only hits with no title overlap.
                if (
                    self._contains_cjk(query.title)
                    and title_overlap < 0.08
                    and not has_strong_episode_signal
                    and not season_pack
                ):
                    return False

            if query.season and season_only and season_only != query.season:
                return False

            if query.season and query.episode:
                if season_episode:
                    season, episode = season_episode
                    if season != query.season or episode != query.episode:
                        return False
                else:
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
                        and not (
                            self._allow_season_pack_for_episode
                            and season_pack
                        )
                    ):
                        # Episodic requests should avoid ambiguous single-file subtitles.
                        return False
        elif query.media_type == "movie":
            # Movie queries should avoid season/episode packs to reduce title collision
            # cases like "National Treasure" (movie) vs "The Lost National Treasure" (series).
            if self._looks_like_tv_candidate(merged):
                return False
            title_overlap = self._title_overlap_score(query.title, merged)
            chinese_overlap = self._chinese_overlap_score(query.title, merged)
            # If both sides contain Chinese words but no overlap, this is usually a wrong movie.
            if chinese_overlap == 0.0 and title_overlap < 0.12:
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

    @classmethod
    def _looks_like_season_pack(cls, candidate: DirectSubtitleCandidate, *, query: SearchRequest) -> bool:
        merged = f"{candidate.title} {candidate.release_name}"
        season = cls._extract_season(merged)
        if query.season and season and season != query.season:
            return False

        ext = Path(urlparse(candidate.download_url).path).suffix.lower()
        if ext not in {".zip", ".rar"} and candidate.provider not in {"subhd", "subhdtw"}:
            return False

        episode = cls._extract_episode_from_text(merged)
        episode_range = cls._extract_episode_range(merged)
        upper_bound = cls._extract_episode_upper_bound(merged)
        has_pack_marker = cls._has_tv_pack_marker(merged)

        # Hard guard: prevent movie-like candidates (no tv markers) from being treated as season packs.
        if episode is None and not episode_range and upper_bound is None and season is None and not has_pack_marker:
            return False

        if episode is not None and query.episode and episode == query.episode:
            return True

        if episode_range and query.episode and episode_range[0] <= query.episode <= episode_range[1]:
            return True

        if upper_bound is not None and query.episode and query.episode <= upper_bound:
            return True

        if has_pack_marker:
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
    def _has_tv_pack_marker(text: str) -> bool:
        return bool(
            re.search(
                r"(?i)\b(complete|全集|合集|全季|全\d+集|season\s*\d+\s*complete|s\d{1,2}\s*complete|更新至)\b",
                text,
            )
        )

    @staticmethod
    def _looks_like_tv_candidate(text: str) -> bool:
        merged = str(text or "")
        if ChineseSubtitleProvider._extract_season_episode(merged):
            return True
        if ChineseSubtitleProvider._extract_season(merged) is not None:
            return True
        if ChineseSubtitleProvider._extract_episode_from_text(merged) is not None:
            return True
        if ChineseSubtitleProvider._extract_episode_range(merged):
            return True
        if ChineseSubtitleProvider._extract_episode_upper_bound(merged) is not None:
            return True

        if re.search(
            r"(?i)\b(episode|season|complete|全集|全季|合集|更新至|s\d{1,2}\s*complete|season\s*\d+\s*complete)\b",
            merged,
        ):
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
        elif candidate.provider in {"subhd", "subhdtw"}:
            score += 24

        if "zh-cn" in candidate.language_tags:
            score += 40
        if "zh-tw" in candidate.language_tags:
            score += 35
        if "zh-cn" in candidate.language_tags and "zh-tw" in candidate.language_tags:
            score += 20

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
        title_overlap = self._title_overlap_score(query.title, merged)
        score += int(round(title_overlap * 70))
        if title_overlap == 0:
            score -= 20

        chinese_overlap = self._chinese_overlap_score(query.title, merged)
        if chinese_overlap > 0:
            score += int(round(chinese_overlap * 40))
        elif chinese_overlap == 0:
            score -= 60

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
            elif self._allow_season_pack_for_episode and self._looks_like_season_pack(candidate, query=query):
                score += 15
            else:
                score -= 50
        elif query.media_type == "movie":
            if self._looks_like_tv_candidate(merged):
                score -= 220
            else:
                score += 20
            if chinese_overlap == 0:
                score -= 120

        if "官方" in candidate.release_name:
            score += 6

        if "bilingual" in candidate.language_tags:
            score += 12

        return score

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text or ""))

    @staticmethod
    def _contains_japanese_kana(text: str) -> bool:
        return bool(re.search(r"[\u3040-\u30ff]", text or ""))

    @staticmethod
    def _normalize_title_text(text: str) -> str:
        lowered = str(text or "").lower()
        lowered = re.sub(r"[._\-]+", " ", lowered)
        lowered = re.sub(r"[\[\](){}]+", " ", lowered)
        lowered = re.sub(r"\s+", " ", lowered).strip()
        return lowered

    @classmethod
    def _title_tokens(cls, text: str) -> set[str]:
        normalized = cls._normalize_title_text(text)
        tokens: set[str] = set()
        for token in re.findall(r"[a-z0-9]{2,}", normalized):
            if token in TITLE_NOISE_TOKENS:
                continue
            tokens.add(token)
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
            tokens.add(chunk)
            if len(chunk) >= 2:
                for i in range(len(chunk) - 1):
                    tokens.add(chunk[i : i + 2])
        return tokens

    @classmethod
    def _title_overlap_score(cls, query_title: str, candidate_text: str) -> float:
        query_tokens = cls._title_tokens(query_title)
        if not query_tokens:
            return 0.0
        candidate_tokens = cls._title_tokens(candidate_text)
        if not candidate_tokens:
            return 0.0

        intersection = len(query_tokens & candidate_tokens)
        recall = intersection / max(1, len(query_tokens))
        precision = intersection / max(1, len(candidate_tokens))
        score = (recall * 0.7) + (precision * 0.3)

        q_compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", cls._normalize_title_text(query_title))
        c_compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", cls._normalize_title_text(candidate_text))
        if len(q_compact) >= 2 and q_compact in c_compact:
            score = max(score, 0.55)

        return min(1.0, score)

    @classmethod
    def _chinese_overlap_score(cls, query_title: str, candidate_text: str) -> float:
        if not cls._contains_cjk(query_title):
            return -1.0
        if not cls._contains_cjk(candidate_text):
            return -1.0

        query_tokens = {
            token
            for token in cls._title_tokens(query_title)
            if re.search(r"[\u4e00-\u9fff]", token)
        }
        candidate_tokens = {
            token
            for token in cls._title_tokens(candidate_text)
            if re.search(r"[\u4e00-\u9fff]", token)
        }
        if not query_tokens or not candidate_tokens:
            return 0.0

        return len(query_tokens & candidate_tokens) / max(1, len(query_tokens))

    @staticmethod
    def _dedupe_candidates(candidates: list[DirectSubtitleCandidate]) -> list[DirectSubtitleCandidate]:
        seen: set[tuple[str, str, str]] = set()
        deduped: list[DirectSubtitleCandidate] = []
        for item in candidates:
            provider_key = item.provider
            subtitle_id_key = item.subtitle_id
            url_key = item.download_url

            if item.provider in {"subhd", "subhdtw"}:
                provider_key = "subhd-family"
                url_key = ""

            key = (provider_key, subtitle_id_key, url_key)
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
