# MoviePilot Subtitle Agent

一个面向中文用户的字幕搜索/下载服务（FastAPI），并提供 MoviePilot 兼容接口。

## 项目目标

很多用户在 Plex/Infuse + OpenSubtitles 场景下，仍然会遇到“缺少中文字幕”。本服务的默认策略是：

- 默认只关注中文字幕（`zh-cn` / `zh-tw`）。
- 优先使用中文源（`assrt`，`subhd` 用于检索提示）。
- OpenSubtitles 作为可选 fallback，而非默认主链路。

## 主要功能

- 电影/剧集字幕搜索。
- 字幕下载到磁盘。
- 字幕流式下载（供插件直接写入目标目录）。
- MoviePilot 兼容 API（可直接被插件调用）。

## 快速启动（本地）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app.main:app --host 0.0.0.0 --port 8178
```

健康检查：

```bash
curl http://127.0.0.1:8178/health
```

## Docker 启动

```bash
cp .env.example .env
docker compose up --build -d
curl http://127.0.0.1:8178/health
docker compose logs -f subtitle-agent
```

停止：

```bash
docker compose down
```

## 代理配置（重要）

如果你的 NAS 出网需要代理（例如访问 `assrt.net`），请在容器环境变量里至少配置：

```yaml
environment:
  HTTP_PROXY: "http://<proxy-host>:999"
  HTTPS_PROXY: "http://<proxy-host>:999"
  http_proxy: "http://<proxy-host>:999"
  https_proxy: "http://<proxy-host>:999"
  NO_PROXY: "127.0.0.1,localhost,<subtitle-agent-host>,<proxy-host>,172.16.0.0/12,10.0.0.0/8"
```

说明：只有 `HTTP_PROXY` 不够，`https://` 请求需要 `HTTPS_PROXY`。

## 标准 API

- `POST /api/v1/subtitles/search`
- `POST /api/v1/subtitles/download`
- `GET /api/v1/subtitles/fetch/{token}`

## MoviePilot 兼容 API

- `POST /api/v1/moviepilot/subtitles/search`
- `GET /api/v1/moviepilot/subtitles/download/{token}`
- `POST /api/v1/moviepilot/subtitles/download`

兼容别名：

- `/api/moviepilot/...`
- `/moviepilot/...`

## MoviePilot 插件示例

插件示例路径：

`moviepilot-plugin-example/plugins.v2/subtitleagentbridge/__init__.py`

## 与插件配套时的关键建议

为避免字幕写入未整理目录（如 `downloads`），请在插件里配置“只扫描刮削后目录”：

- `include_paths`: 例如 `/media/tv,/media/movies`
- `exclude_paths`: 例如 `/media/downloads,/media/整理前,/media/刷流`
- `exclude_keywords`: 默认已包含 `downloads,download,整理前,刷流,strm,stream`

## 更新记录（近期）

- `v0.1.1`：修复中文文件名在下载响应头中导致的 500 错误（`Content-Disposition` 编码兼容）。

## 测试

```bash
pytest -q
```
