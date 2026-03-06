import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MediaType


class SubtitleAgentBridge(_PluginBase):
    """
    MoviePilot 插件示例：对接外部 Subtitle Agent，自动为入库媒体下载字幕。
    """

    plugin_name = "Subtitle Agent Bridge"
    plugin_desc = "调用外部 MoviePilot Subtitle Agent 自动检索并下载字幕。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "0.5.1"
    plugin_author = "jun9100"
    author_url = "https://github.com/jun9100/moviepilot-subtitleagentbridge"
    plugin_config_prefix = "subtitleagentbridge_"
    plugin_order = 50
    auth_level = 1

    _enabled: bool = False
    _host: str = ""
    _search_path: str = "/api/v1/moviepilot/subtitles/search"
    _languages: str = "zh-cn,zh-tw"
    _limit: int = 5
    _timeout: int = 60
    _overwrite: bool = False
    _notify: bool = True
    _include_paths: str = ""
    _exclude_paths: str = ""
    _exclude_keywords: str = "整理前,刷流,strm,stream,downloads,download,incoming,temp,cache"
    _title_aliases: str = ""

    _subtitle_suffixes = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
    _season_episode_pattern = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
    _year_pattern = re.compile(r"\b(19\d{2}|20\d{2})\b")

    def init_plugin(self, config: dict = None):
        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._host = self.__normalize_host(config.get("host"))
        self._search_path = str(config.get("search_path") or "/api/v1/moviepilot/subtitles/search")
        self._languages = str(config.get("languages") or "zh-cn,zh-tw")
        self._limit = int(config.get("limit") or 5)
        # Some providers may respond slowly in NAS environments; keep a safe minimum timeout.
        self._timeout = max(int(config.get("timeout") or 60), 60)
        self._overwrite = bool(config.get("overwrite"))
        self._notify = bool(config.get("notify", True))
        self._include_paths = str(config.get("include_paths") or "")
        self._exclude_paths = str(config.get("exclude_paths") or "")
        self._exclude_keywords = str(
            config.get("exclude_keywords") or "整理前,刷流,strm,stream,downloads,download,incoming,temp,cache"
        )
        self._title_aliases = str(config.get("title_aliases") or "")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/download_subtitle",
                "endpoint": self.download_subtitle,
                "methods": ["GET"],
                "summary": "手动下载字幕",
                "description": "按标题信息调用 Subtitle Agent 搜索并下载字幕。",
            },
            {
                "path": "/backfill_directory",
                "endpoint": self.backfill_directory,
                "methods": ["GET"],
                "summary": "补齐目录字幕",
                "description": "扫描目录中缺失字幕的视频文件并批量下载字幕。支持仅扫描刮削后目录，并排除整理前/刷流目录。",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "overwrite", "label": "覆盖已有字幕"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送结果通知"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "include_paths",
                                            "label": "仅扫描目录(刮削后)",
                                            "placeholder": "/media/library/tv,/media/library/movie",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude_paths",
                                            "label": "回填排除目录",
                                            "placeholder": "/media/downloads,/media/整理前,/media/刷流",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "title_aliases",
                                            "label": "标题别名映射",
                                            "placeholder": "短剧开始啦=コントが始まる; 黄石：法警小队=Marshals",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude_keywords",
                                            "label": "回填排除关键词",
                                            "placeholder": "整理前,刷流,strm,stream,downloads,download,incoming,temp,cache",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "host",
                                            "label": "Subtitle Agent 地址",
                                            "placeholder": "http://127.0.0.1:8178",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "search_path",
                                            "label": "搜索接口路径",
                                            "placeholder": "/api/v1/moviepilot/subtitles/search",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "languages",
                                            "label": "字幕语言",
                                            "placeholder": "zh-cn,zh-tw",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "limit",
                                            "label": "单文件候选数量",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "timeout",
                                            "label": "HTTP超时(秒)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "插件监听 transfer.complete 事件。建议配置“仅扫描目录”为刮削后媒体库路径，避免字幕写入下载/未整理目录。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "host": "http://127.0.0.1:8178",
            "search_path": "/api/v1/moviepilot/subtitles/search",
            "languages": "zh-cn,zh-tw",
            "limit": 5,
            "timeout": 60,
            "overwrite": False,
            "notify": True,
            "include_paths": "",
            "exclude_paths": "",
            "exclude_keywords": "整理前,刷流,strm,stream,downloads,download,incoming,temp,cache",
            "title_aliases": "",
        }

    def get_page(self) -> List[dict]:
        last_result = self.get_data("last_result") or {}

        lines = [
            f"最近执行时间: {last_result.get('time') or '-'}",
            f"处理文件数: {last_result.get('total') or 0}",
            f"成功数: {last_result.get('success') or 0}",
            f"跳过数: {last_result.get('skipped') or 0}",
            f"排除数: {last_result.get('excluded') or 0}",
            f"失败数: {last_result.get('failed') or 0}",
        ]
        errors = last_result.get("errors") or []
        if errors:
            lines.append("失败详情:")
            lines.extend(errors[:10])

        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "\n".join(lines),
                                },
                            }
                        ],
                    }
                ],
            }
        ]

    def stop_service(self):
        pass

    @eventmanager.register(EventType.TransferComplete)
    def download_on_transfer_complete(self, event: Event):
        if not self._enabled:
            return
        if not self._host:
            logger.warn("[SubtitleAgentBridge] 未配置 Subtitle Agent 地址")
            return

        event_data = event.event_data or {}
        mediainfo = event_data.get("mediainfo")
        transferinfo = event_data.get("transferinfo")
        meta = event_data.get("meta")

        if not mediainfo or not transferinfo:
            return

        file_list = getattr(transferinfo, "file_list_new", None) or []
        if not file_list:
            return

        total = 0
        success = 0
        errors = []
        include_paths = self.__merge_csv_values(self._include_paths)
        excluded_paths = self.__merge_csv_values(self._exclude_paths)
        excluded_keywords = self.__merge_csv_values(self._exclude_keywords)

        for media_file in file_list:
            if not self.__is_video_file(media_file):
                continue
            media_path = Path(media_file)
            if not self.__is_included_path(media_path, include_paths):
                logger.info(f"[SubtitleAgentBridge] 跳过非库目录文件: {media_file}")
                continue
            if self.__is_excluded_path(media_path, excluded_paths, excluded_keywords):
                logger.info(f"[SubtitleAgentBridge] 跳过排除目录文件: {media_file}")
                continue
            total += 1
            state, message = self.__download_for_media_file(
                media_file=media_file,
                mediainfo=mediainfo,
                meta=meta,
            )
            if state:
                success += 1
            else:
                errors.append(f"{Path(media_file).name}: {message}")

        if total == 0:
            return

        failed = max(total - success, 0)
        result = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": total,
            "success": success,
            "failed": failed,
            "errors": errors,
        }
        self.save_data("last_result", result)

        if self._notify:
            text = f"处理 {total} 个视频，成功 {success} 个，失败 {failed} 个"
            if errors:
                text = f"{text}\n" + "\n".join(errors[:5])
            self.post_message(
                mtype=NotificationType.Plugin,
                title="Subtitle Agent 字幕下载结果",
                text=text,
            )

    def download_subtitle(
        self,
        title: str,
        apikey: str,
        media_type: str = "movie",
        year: int = None,
        season: int = None,
        episode: int = None,
        target_file: str = "",
        languages: str = "",
    ) -> schemas.Response:
        """
        手动触发字幕下载，可由插件API调用：
        /api/v1/plugin/SubtitleAgentBridge/download_subtitle
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")

        payload = {
            "title": title,
            "type": "series" if media_type in ["tv", "series", "show"] else "movie",
            "year": year,
            "season": season,
            "episode": episode,
            "language": languages or self._languages,
            "limit": self._limit,
        }

        items, message = self.__search_items(payload)
        if not items:
            return schemas.Response(
                success=False,
                message=self.__normalize_failure_message(message, "未找到可用字幕"),
            )

        selected = self.__pick_item(
            items,
            preferred_languages=self.__split_languages(languages or self._languages),
        )
        content, subtitle_format, message = self.__download_item(selected)
        if not content:
            return schemas.Response(success=False, message=message or "下载字幕失败")

        if not target_file:
            return schemas.Response(
                success=True,
                message="字幕下载成功（未写入文件，因 target_file 为空）",
                data={
                    "provider": selected.get("provider"),
                    "subtitle_id": selected.get("subtitle_id"),
                    "size": len(content),
                },
            )

        subtitle_path = self.__build_subtitle_path(target_file, subtitle_format)
        try:
            subtitle_file = Path(subtitle_path)
            subtitle_file.parent.mkdir(parents=True, exist_ok=True)
            subtitle_file.write_bytes(content)
        except Exception as err:
            return schemas.Response(success=False, message=f"写入字幕失败: {str(err)}")

        return schemas.Response(
            success=True,
            message=f"字幕下载完成: {subtitle_path}",
            data={
                "path": subtitle_path,
                "provider": selected.get("provider"),
                "subtitle_id": selected.get("subtitle_id"),
                "size": len(content),
            },
        )

    def backfill_directory(
        self,
        apikey: str,
        directory: str,
        recursive: bool = True,
        media_type: str = "",
        languages: str = "",
        name_contains: str = "",
        include_paths: str = "",
        exclude_paths: str = "",
        exclude_keywords: str = "",
        title_aliases: str = "",
        overwrite: bool = False,
        max_files: int = 200,
        limit: int = 0,
    ) -> schemas.Response:
        """
        扫描目录里没有字幕的视频文件并批量下载字幕。
        API:
        /api/v1/plugin/SubtitleAgentBridge/backfill_directory
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        if not self._host:
            return schemas.Response(success=False, message="未配置 Subtitle Agent 地址")
        if not directory:
            return schemas.Response(success=False, message="directory 参数不能为空")

        recursive_flag = self.__to_bool(recursive, default=True)
        overwrite_flag = self.__to_bool(overwrite, default=self._overwrite)
        desired_type = self.__normalize_media_type(media_type)
        preferred_languages = self.__split_languages(languages or self._languages)
        selected_languages = ",".join(preferred_languages) if preferred_languages else self._languages
        effective_limit = self.__to_int(limit) or self._limit
        if effective_limit <= 0:
            effective_limit = self._limit

        max_file_count = self.__to_int(max_files) or 200
        if max_file_count <= 0:
            max_file_count = 200
        name_keyword = str(name_contains or "").strip()
        name_filter = name_keyword.lower()
        included_paths = self.__merge_csv_values(self._include_paths, include_paths)
        excluded_paths = self.__merge_csv_values(self._exclude_paths, exclude_paths)
        excluded_keywords = self.__merge_csv_values(self._exclude_keywords, exclude_keywords)
        alias_map = self.__merge_title_aliases(self._title_aliases, title_aliases)
        scan_roots = self.__collect_scan_roots(directory=directory, include_paths=included_paths)
        if not scan_roots:
            return schemas.Response(success=False, message="没有可扫描目录，请检查 directory/include_paths 配置")

        processed = 0
        success = 0
        skipped = 0
        excluded = 0
        failed = 0
        errors: List[str] = []
        downloaded: List[Dict[str, Any]] = []

        matched = 0
        seen_identities = set()
        try:
            for scan_root in scan_roots:
                file_iter = self.__iter_video_files(scan_root, recursive=recursive_flag)
                for video_file in file_iter:
                    identity = self.__file_identity(video_file)
                    if identity and identity in seen_identities:
                        continue
                    if identity:
                        seen_identities.add(identity)

                    if self.__is_excluded_path(
                        media_file=video_file,
                        excluded_paths=excluded_paths,
                        excluded_keywords=excluded_keywords,
                    ):
                        excluded += 1
                        continue

                    if name_filter and name_filter not in video_file.name.lower():
                        continue

                    matched += 1
                    if matched > max_file_count:
                        break

                    processed += 1

                    if not overwrite_flag and self.__has_subtitle(video_file):
                        skipped += 1
                        continue

                    parsed = self.__parse_media_context_from_file(video_file, forced_media_type=desired_type)
                    if not parsed.get("title"):
                        failed += 1
                        errors.append(f"{video_file.name}: 无法从文件名解析标题")
                        continue

                    payload = {
                        "title": parsed.get("title"),
                        "type": parsed.get("type"),
                        "year": parsed.get("year"),
                        "season": parsed.get("season"),
                        "episode": parsed.get("episode"),
                        "language": selected_languages,
                        "limit": effective_limit,
                    }

                    selected_title = payload.get("title")
                    items = []
                    message = None
                    for title_candidate in self.__build_title_candidates(
                        parsed_title=parsed.get("title"),
                        media_file=video_file,
                        name_keyword=name_keyword,
                        alias_map=alias_map,
                    ):
                        payload["title"] = title_candidate
                        items, message = self.__search_items(payload)
                        if items:
                            selected_title = title_candidate
                            break

                    if not items:
                        failed += 1
                        errors.append(
                            f"{video_file.name}: {self.__normalize_failure_message(message, '未找到字幕')}"
                        )
                        continue

                    selected = self.__pick_item(items, preferred_languages=preferred_languages)
                    content, subtitle_format, message = self.__download_item(selected)
                    if not content:
                        failed += 1
                        errors.append(
                            f"{video_file.name}: {self.__normalize_failure_message(message, '下载字幕失败')}"
                        )
                        continue

                    subtitle_path = Path(self.__build_subtitle_path(str(video_file), subtitle_format))
                    if subtitle_path.exists() and not overwrite_flag:
                        skipped += 1
                        continue

                    try:
                        subtitle_path.parent.mkdir(parents=True, exist_ok=True)
                        subtitle_path.write_bytes(content)
                    except Exception as err:
                        failed += 1
                        errors.append(f"{video_file.name}: 写入字幕失败: {str(err)}")
                        continue

                    success += 1
                    downloaded.append(
                        {
                            "video": str(video_file),
                            "subtitle": str(subtitle_path),
                            "title": selected_title,
                            "provider": selected.get("provider"),
                            "language": selected.get("language"),
                        }
                    )
                    logger.info(f"[SubtitleAgentBridge] 批量补字幕成功: {subtitle_path}")

                if matched > max_file_count:
                    break
        except Exception as err:
            return schemas.Response(success=False, message=f"扫描目录失败: {str(err)}")

        if processed == 0:
            if name_filter:
                return schemas.Response(success=False, message=f"目录中未找到匹配关键词的视频文件: {name_filter}")
            return schemas.Response(success=False, message="目录中未找到视频文件")

        result = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": processed,
            "success": success,
            "skipped": skipped,
            "excluded": excluded,
            "failed": failed,
            "errors": errors,
        }
        self.save_data("last_result", result)

        if self._notify:
            text = (
                f"补字幕完成，共扫描 {processed} 个视频，成功 {success} 个，"
                f"跳过 {skipped} 个，排除 {excluded} 个，失败 {failed} 个"
            )
            if errors:
                text = f"{text}\n" + "\n".join(errors[:5])
            self.post_message(
                mtype=NotificationType.Plugin,
                title="Subtitle Agent 补字幕结果",
                text=text,
            )

        message = f"扫描 {processed} 个视频，成功 {success}，跳过 {skipped}，排除 {excluded}，失败 {failed}"
        return schemas.Response(
            success=failed == 0,
            message=message,
            data={
                "directory": directory,
                "scan_roots": [str(path) for path in scan_roots],
                "recursive": recursive_flag,
                "overwrite": overwrite_flag,
                "name_contains": name_filter,
                "include_paths": included_paths,
                "exclude_paths": excluded_paths,
                "exclude_keywords": excluded_keywords,
                "languages": selected_languages,
                "total": processed,
                "success": success,
                "skipped": skipped,
                "excluded": excluded,
                "failed": failed,
                "items": downloaded[:50],
                "errors": errors[:50],
            },
        )

    def __download_for_media_file(self, media_file: str, mediainfo: Any, meta: Any) -> Tuple[bool, str]:
        payload = self.__build_search_payload(mediainfo=mediainfo, meta=meta)
        items, message = self.__search_items(payload)
        if not items:
            return False, self.__normalize_failure_message(message, "未找到字幕")

        selected = self.__pick_item(items)
        content, subtitle_format, message = self.__download_item(selected)
        if not content:
            return False, message or "下载失败"

        subtitle_path = self.__build_subtitle_path(media_file, subtitle_format)
        subtitle_file = Path(subtitle_path)

        if subtitle_file.exists() and not self._overwrite:
            return False, "字幕已存在且未启用覆盖"

        try:
            subtitle_file.write_bytes(content)
        except Exception as err:
            return False, f"写入字幕失败: {str(err)}"

        logger.info(f"[SubtitleAgentBridge] 字幕下载完成: {subtitle_file}")
        return True, subtitle_path

    def __build_search_payload(self, mediainfo: Any, meta: Any) -> Dict[str, Any]:
        media_type = "movie"
        if getattr(mediainfo, "type", None) == MediaType.TV:
            media_type = "series"

        season = getattr(mediainfo, "season", None)
        episode = getattr(meta, "begin_episode", None) if meta else None
        if not season and meta:
            season = getattr(meta, "begin_season", None)

        payload = {
            "title": getattr(mediainfo, "title", None) or getattr(mediainfo, "en_title", None),
            "type": media_type,
            "year": self.__to_int(getattr(mediainfo, "year", None)),
            "season": self.__to_int(season),
            "episode": self.__to_int(episode),
            "imdbid": getattr(mediainfo, "imdb_id", None),
            "tmdbid": getattr(mediainfo, "tmdb_id", None),
            "language": self._languages,
            "limit": self._limit,
        }
        return payload

    def __search_items(self, payload: Dict[str, Any]) -> Tuple[List[dict], Optional[str]]:
        search_url = self.__compose_url(self._search_path)
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url=search_url,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self._timeout) as res:
                status_code = int(getattr(res, "status", 200))
                content = res.read()
        except Exception as err:
            return [], f"请求搜索接口失败: {str(err)}"

        if status_code != 200:
            return [], f"搜索接口返回错误: {status_code}"

        try:
            body = json.loads(content.decode("utf-8", errors="ignore"))
        except Exception as err:
            return [], f"解析搜索结果失败: {str(err)}"

        # 兼容 MoviePilot 包装格式
        if isinstance(body, dict) and "success" in body:
            if body.get("success") is True and isinstance(body.get("data"), dict):
                items = body.get("data", {}).get("items") or []
                item_list = items if isinstance(items, list) else []
                if item_list:
                    return item_list, body.get("message")
                return [], str(body.get("message") or "未找到字幕")
            return [], str(body.get("message") or "字幕检索失败")

        # 兼容标准格式
        items = body.get("items") if isinstance(body, dict) else []
        return items if isinstance(items, list) else [], None

    def __pick_item(self, items: List[dict], preferred_languages: Optional[List[str]] = None) -> dict:
        preferred_langs = preferred_languages or self.__split_languages(self._languages)
        for lang in preferred_langs:
            for item in items:
                if str(item.get("language") or "").lower().startswith(lang.lower()):
                    return item
        return items[0]

    def __download_item(self, item: dict) -> Tuple[Optional[bytes], str, Optional[str]]:
        download_url = item.get("download_url")
        if not download_url:
            return None, "srt", "候选字幕缺少下载地址"

        full_url = self.__compose_url(download_url)
        try:
            request = Request(
                url=full_url,
                headers={"Accept": "*/*"},
                method="GET",
            )
            with urlopen(request, timeout=self._timeout) as res:
                status_code = int(getattr(res, "status", 200))
                content_type = str(res.headers.get("Content-Type") or "").lower()
                content = res.read()
        except Exception as err:
            return None, "srt", f"请求下载接口失败: {str(err)}"

        if status_code != 200:
            return None, "srt", f"下载接口返回错误: {status_code}"

        if "application/json" in content_type:
            try:
                body = json.loads(content.decode("utf-8", errors="ignore"))
            except Exception:
                body = None
            if isinstance(body, dict) and "success" in body and body.get("success") is not True:
                return None, "srt", str(body.get("message") or "字幕下载失败")

        if not content:
            return None, "srt", "下载内容为空"

        subtitle_format = str(item.get("format") or item.get("subtitle_format") or "srt").lower()
        return content, subtitle_format, None

    @staticmethod
    def __is_video_file(path: str) -> bool:
        ext = Path(path).suffix.lower()
        return ext in {
            ".mp4",
            ".mkv",
            ".avi",
            ".wmv",
            ".flv",
            ".mov",
            ".m4v",
            ".ts",
            ".m2ts",
            ".webm",
        }

    def __iter_video_files(self, root: Path, recursive: bool) -> Iterator[Path]:
        iterator = root.rglob("*") if recursive else root.iterdir()
        for path in iterator:
            if not path.is_file():
                continue
            if not self.__is_video_file(str(path)):
                continue
            yield path

    def __has_subtitle(self, media_file: Path) -> bool:
        prefix = media_file.stem
        subtitle_prefix = f"{prefix}."
        for candidate in media_file.parent.iterdir():
            if not candidate.is_file():
                continue
            suffix = candidate.suffix.lower()
            if suffix not in self._subtitle_suffixes:
                continue
            name = candidate.name
            if name == f"{prefix}{suffix}" or name.startswith(subtitle_prefix):
                return True
        return False

    def __build_title_candidates(
        self,
        parsed_title: Any,
        media_file: Path,
        name_keyword: str = "",
        alias_map: Optional[Dict[str, List[str]]] = None,
    ) -> List[str]:
        candidates: List[str] = []
        seen = set()

        keyword = name_keyword if self.__should_use_name_keyword(name_keyword) else ""
        alias_titles = self.__lookup_alias_titles(parsed_title=parsed_title, media_file=media_file, alias_map=alias_map or {})
        series_title = self.__extract_series_title(media_file)

        for raw_title in [parsed_title, *alias_titles, series_title, keyword, media_file.stem]:
            normalized = self.__clean_title_text(str(raw_title or ""))
            if not normalized:
                continue
            if self.__is_generic_title(normalized):
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(normalized)

        return candidates

    @staticmethod
    def __should_use_name_keyword(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        compact = re.sub(r"\s+", "", text)
        return len(compact) >= 3

    def __clean_title_text(self, title: str) -> str:
        value = str(title or "")
        value = re.sub(r"[\[\(\{][^\]\)\}]*[\]\)\}]", " ", value)
        value = self._season_episode_pattern.sub(" ", value)
        value = re.sub(r"(?i)\bS\d{1,2}\b", " ", value)
        value = re.sub(r"(?i)\bseason\s*\d+\b", " ", value)
        value = re.sub(r"第\s*\d+\s*[季集话]", " ", value)
        value = self._year_pattern.sub(" ", value)
        value = re.sub(
            r"(?i)\b(2160p|1080p|720p|480p|x264|x265|h264|h265|hevc|hdr|dv|bluray|bdrip|web[-_. ]?dl|webrip|hdrip|dvdrip|aac|dts|atmos|remux|repack|proper|extended|paramount\+?|netflix|amzn|hmax|hbo|max|disney\+?|atvp)\b",
            " ",
            value,
        )
        value = re.sub(r"(?i)\b\d+audios?\b", " ", value)
        value = re.sub(r"-[A-Za-z0-9]{2,}$", " ", value)
        value = re.sub(r"[._]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip(" -._")
        return value

    @staticmethod
    def __is_generic_title(value: str) -> bool:
        text = str(value or "").strip().lower()
        if not text:
            return True
        if re.fullmatch(r"season\s*\d+", text):
            return True
        if re.fullmatch(r"s\d{1,2}", text):
            return True
        if re.fullmatch(r"第\s*\d+\s*[季集话]", text):
            return True
        if text in {"movie", "tv", "series"}:
            return True
        return False

    def __extract_series_title(self, media_file: Path) -> str:
        for parent in media_file.parents[:3]:
            title = self.__clean_title_text(parent.name)
            if not title:
                continue
            if self.__is_generic_title(title):
                continue
            return title
        return ""

    def __parse_media_context_from_file(self, media_file: Path, forced_media_type: str = "") -> Dict[str, Any]:
        raw_name = media_file.stem
        cleaned = re.sub(r"[\[\(\{][^\]\)\}]*[\]\)\}]", " ", raw_name)

        season = None
        episode = None
        season_episode_match = self._season_episode_pattern.search(cleaned)
        if season_episode_match:
            season = self.__to_int(season_episode_match.group(1))
            episode = self.__to_int(season_episode_match.group(2))

        media_kind = self.__normalize_media_type(forced_media_type)
        if not media_kind:
            media_kind = "series" if season and episode else "movie"

        year = None
        year_match = self._year_pattern.search(raw_name) or self._year_pattern.search(cleaned)
        if year_match:
            year = self.__to_int(year_match.group(1))

        normalized_title = self.__clean_title_text(cleaned)

        if not normalized_title:
            normalized_title = self.__clean_title_text(media_file.parent.name)

        return {
            "title": normalized_title,
            "type": media_kind,
            "year": year,
            "season": season if media_kind == "series" else None,
            "episode": episode if media_kind == "series" else None,
        }

    @staticmethod
    def __build_subtitle_path(media_file: str, subtitle_format: str) -> str:
        fmt = (subtitle_format or "srt").strip().lower()
        if not fmt:
            fmt = "srt"
        return str(Path(media_file).with_suffix(f".{fmt}"))

    @staticmethod
    def __split_csv(value: Any) -> List[str]:
        return [item.strip() for item in str(value or "").split(",") if item and item.strip()]

    @staticmethod
    def __merge_csv_values(*values: Any) -> List[str]:
        merged: List[str] = []
        seen = set()
        for value in values:
            for item in SubtitleAgentBridge.__split_csv(value):
                key = item.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(item.strip())
        return merged

    def __merge_title_aliases(self, *values: Any) -> Dict[str, List[str]]:
        alias_map: Dict[str, List[str]] = {}
        for value in values:
            if not value:
                continue
            chunks = re.split(r"[;\n]+", str(value))
            for chunk in chunks:
                pair = str(chunk or "").strip()
                if not pair:
                    continue
                key, sep, raw_values = pair.partition("=")
                if not sep:
                    key, sep, raw_values = pair.partition(":")
                if not sep:
                    continue
                source = self.__clean_title_text(key)
                if not source:
                    continue
                aliases = [
                    self.__clean_title_text(item)
                    for item in re.split(r"[|,]+", raw_values)
                    if self.__clean_title_text(item)
                ]
                if not aliases:
                    continue
                alias_map[source.lower()] = aliases
        return alias_map

    def __lookup_alias_titles(self, parsed_title: Any, media_file: Path, alias_map: Dict[str, List[str]]) -> List[str]:
        if not alias_map:
            return []

        keys = []
        parsed = self.__clean_title_text(parsed_title)
        series = self.__extract_series_title(media_file)
        if parsed:
            keys.append(parsed.lower())
        if series:
            keys.append(series.lower())

        aliases: List[str] = []
        seen = set()
        for key in keys:
            for item in alias_map.get(key, []):
                lowered = item.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                aliases.append(item)
        return aliases

    def __collect_scan_roots(self, directory: str, include_paths: List[str]) -> List[Path]:
        candidates = include_paths if include_paths else [directory]
        roots: List[Path] = []
        seen = set()
        for item in candidates:
            path = Path(str(item)).expanduser()
            if not path.exists() or not path.is_dir():
                continue
            key = self.__normalize_path(str(path))
            if key in seen:
                continue
            seen.add(key)
            roots.append(path)
        return roots

    @staticmethod
    def __split_languages(languages: str) -> List[str]:
        return SubtitleAgentBridge.__split_csv(languages)

    @staticmethod
    def __file_identity(path: Path) -> str:
        try:
            stat = path.stat()
            return f"{stat.st_dev}:{stat.st_ino}"
        except Exception:
            return ""

    def __is_included_path(self, media_file: Path, include_paths: List[str]) -> bool:
        if not include_paths:
            return True
        normalized_file = self.__normalize_path(str(media_file))
        for raw_path in include_paths:
            path_prefix = self.__normalize_path(raw_path)
            if not path_prefix:
                continue
            if normalized_file == path_prefix or normalized_file.startswith(f"{path_prefix}/"):
                return True
        return False

    def __is_excluded_path(self, media_file: Path, excluded_paths: List[str], excluded_keywords: List[str]) -> bool:
        normalized_file = self.__normalize_path(str(media_file))
        for raw_path in excluded_paths:
            path_prefix = self.__normalize_path(raw_path)
            if not path_prefix:
                continue
            if normalized_file == path_prefix or normalized_file.startswith(f"{path_prefix}/"):
                return True

        for keyword in excluded_keywords:
            key = str(keyword or "").strip().lower()
            if key and key in normalized_file:
                return True
        return False

    @staticmethod
    def __normalize_failure_message(message: Any, default: str) -> str:
        text = str(message or "").strip()
        if not text or text.lower() in {"ok", "success", "none"}:
            return default
        return text

    @staticmethod
    def __to_int(value: Any) -> Optional[int]:
        if value in [None, "", 0, "0"]:
            return None
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def __to_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n"}:
            return False
        return default

    @staticmethod
    def __normalize_media_type(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"series", "tv", "show", "episode"}:
            return "series"
        if text in {"movie", "film"}:
            return "movie"
        return ""

    @staticmethod
    def __normalize_path(value: Any) -> str:
        return str(value or "").replace("\\", "/").strip().rstrip("/").lower()

    @staticmethod
    def __normalize_host(host: Any) -> str:
        if not host:
            return ""
        value = str(host).strip()
        if not value:
            return ""
        if not value.startswith("http"):
            value = f"http://{value}"
        if value.endswith("/"):
            value = value[:-1]
        return value

    def __compose_url(self, path: str) -> str:
        if str(path).startswith("http://") or str(path).startswith("https://"):
            return str(path)
        if not self._host:
            return str(path)
        return urljoin(f"{self._host}/", str(path).lstrip("/"))
