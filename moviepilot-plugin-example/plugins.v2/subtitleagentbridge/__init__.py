from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MediaType
from app.utils.http import RequestUtils


class SubtitleAgentBridge(_PluginBase):
    """
    MoviePilot 插件示例：对接外部 Subtitle Agent，自动为入库媒体下载字幕。
    """

    plugin_name = "Subtitle Agent Bridge"
    plugin_desc = "调用外部 MoviePilot Subtitle Agent 自动检索并下载字幕。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "0.1.0"
    plugin_author = "codex"
    author_url = "https://github.com/openai"
    plugin_config_prefix = "subtitleagentbridge_"
    plugin_order = 50
    auth_level = 1

    _enabled: bool = False
    _host: str = ""
    _search_path: str = "/api/v1/moviepilot/subtitles/search"
    _languages: str = "zh,en"
    _limit: int = 5
    _timeout: int = 20
    _overwrite: bool = False
    _notify: bool = True

    def init_plugin(self, config: dict = None):
        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._host = self.__normalize_host(config.get("host"))
        self._search_path = str(config.get("search_path") or "/api/v1/moviepilot/subtitles/search")
        self._languages = str(config.get("languages") or "zh,en")
        self._limit = int(config.get("limit") or 5)
        self._timeout = int(config.get("timeout") or 20)
        self._overwrite = bool(config.get("overwrite"))
        self._notify = bool(config.get("notify", True))

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/download_subtitle",
                "endpoint": self.download_subtitle,
                "methods": ["GET"],
                "summary": "手动下载字幕",
                "description": "按标题信息调用 Subtitle Agent 搜索并下载字幕。",
            }
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
                                            "placeholder": "zh,en",
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
                                            "text": "插件监听 transfer.complete 事件。媒体入库后，会调用 Subtitle Agent 的 MoviePilot 兼容接口并将字幕写入同目录。",
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
            "languages": "zh,en",
            "limit": 5,
            "timeout": 20,
            "overwrite": False,
            "notify": True,
        }

    def get_page(self) -> List[dict]:
        last_result = self.get_data("last_result") or {}

        lines = [
            f"最近执行时间: {last_result.get('time') or '-'}",
            f"处理文件数: {last_result.get('total') or 0}",
            f"成功数: {last_result.get('success') or 0}",
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

        for media_file in file_list:
            if not self.__is_video_file(media_file):
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
            "time": Path(__file__).stat().st_mtime,
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
            return schemas.Response(success=False, message=message or "未找到可用字幕")

        selected = items[0]
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
            Path(subtitle_path).write_bytes(content)
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

    def __download_for_media_file(self, media_file: str, mediainfo: Any, meta: Any) -> Tuple[bool, str]:
        payload = self.__build_search_payload(mediainfo=mediainfo, meta=meta)
        items, message = self.__search_items(payload)
        if not items:
            return False, message or "未找到字幕"

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

        try:
            res = RequestUtils(timeout=self._timeout).post(search_url, json=payload)
        except Exception as err:
            return [], f"请求搜索接口失败: {str(err)}"

        if not res:
            return [], "搜索接口无响应"
        if res.status_code != 200:
            return [], f"搜索接口返回错误: {res.status_code}"

        try:
            body = res.json()
        except Exception as err:
            return [], f"解析搜索结果失败: {str(err)}"

        # 兼容 MoviePilot 包装格式
        if isinstance(body, dict) and body.get("success") is True and isinstance(body.get("data"), dict):
            items = body.get("data", {}).get("items") or []
            return items if isinstance(items, list) else [], body.get("message")

        # 兼容标准格式
        items = body.get("items") if isinstance(body, dict) else []
        return items if isinstance(items, list) else [], None

    def __pick_item(self, items: List[dict]) -> dict:
        preferred_langs = self.__split_languages(self._languages)
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
            res = RequestUtils(timeout=self._timeout).get_res(full_url)
        except Exception as err:
            return None, "srt", f"请求下载接口失败: {str(err)}"

        if not res:
            return None, "srt", "下载接口无响应"
        if res.status_code != 200:
            return None, "srt", f"下载接口返回错误: {res.status_code}"

        content = res.content
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

    @staticmethod
    def __build_subtitle_path(media_file: str, subtitle_format: str) -> str:
        fmt = (subtitle_format or "srt").strip().lower()
        if not fmt:
            fmt = "srt"
        return str(Path(media_file).with_suffix(f".{fmt}"))

    @staticmethod
    def __split_languages(languages: str) -> List[str]:
        return [item.strip() for item in str(languages).split(",") if item and item.strip()]

    @staticmethod
    def __to_int(value: Any) -> Optional[int]:
        if value in [None, "", 0, "0"]:
            return None
        try:
            return int(value)
        except Exception:
            return None

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
