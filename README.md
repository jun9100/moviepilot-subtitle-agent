# MoviePilot Subtitle Agent

一个面向中文用户的字幕搜索/下载服务（FastAPI），并提供 MoviePilot 兼容接口。

## 项目目标

很多用户在 Plex/Infuse + OpenSubtitles 场景下，仍然会遇到“缺少中文字幕”。本服务的默认策略是：

- 默认只关注中文字幕（`zh-cn` / `zh-tw`）。
- 优先使用中文源（`assrt` + `subhd` + `subhdtw`，均支持检索与下载）。
- 多源分层检索：`assrt/subhd/subhdtw` → `podnapisi/tvsubtitles` → `opensubtitles`（可配置，不写死）。
- OpenSubtitles 仅作为最后兜底。
- 下载自动重试：先换同阶段候选，再自动进入下一阶段源，减少“搜到但下不下来”。
- 阶段内并发检索：同一层多个 provider 并发查找，缩短慢站点等待时间。

## 主要功能

- 电影/剧集字幕搜索。
- 剧集集数匹配增强：支持 `SxxEyy`、`E01-E06`、`更新至E06`、整季/合集压缩包。
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

## 检索顺序与精度（可配置）

### 1) 自定义检索分层顺序

环境变量：`PROVIDER_STAGE_ORDER`

- 语法：每一层用 `|` 分隔，层内 provider 用 `,` 分隔。
- 示例（推荐：中文源优先，OpenSubtitles 最后）：

```env
PROVIDER_STAGE_ORDER=assrt,subhd,subhdtw|podnapisi,tvsubtitles|opensubtitlescom,opensubtitles
```

### 2) 错剧/错集控制

- `MIN_SCORE`：最低候选分数（默认 `0`），可提高到 `60`~`120` 过滤低质量匹配。
- `ALLOW_SEASON_PACK_FOR_EPISODE`：
  - `true`（默认）：允许整季包用于单集请求（命中率更高）。
  - `false`：仅接受更严格的单集匹配（准确率更高）。
- `STRICT_MEDIA_TYPE_FILTER`：
  - `true`（默认）：对 `movie/tv` 启用强约束，避免电影请求混入剧集候选、剧集请求混入电影候选。
  - `false`：回退为更宽松匹配策略（命中率更高但误匹配风险上升）。

### 3) 内容级中文置信度校验（可配置）

- `ENABLE_CONTENT_LANGUAGE_VALIDATION`：默认 `true`，下载后按字幕正文评估中文置信度。
- `CHINESE_CONFIDENCE_THRESHOLD`：默认 `0.25`，越高越严格。
- `CHINESE_CONFIDENCE_MIN_CHARS`：默认 `4`，至少需要的中文字符数。

### 4) 阶段内并发搜索（可配置）

- `ENABLE_PARALLEL_SEARCH`：是否启用同阶段 provider 并发检索（默认 `true`）。
- `SEARCH_WORKERS`：并发线程数（默认 `6`）。

### 5) subhd 验证码干扰缓解（可配置）

- `SUBHD_CAPTCHA_COOLDOWN_SECONDS`：subhd 返回验证码后，镜像进入冷却时间（默认 `1800` 秒），避免反复触发验证码。
- `SUBHD_COOKIE_STRING`：手动注入 Cookie 字符串（例如 `cf_clearance=...; session=...`），会自动应用到 `subhd.tv/subhdtw.com/subhd.cc/subhd.me`。
- `SUBHD_COOKIE_FILE`：Netscape 格式 `cookies.txt` 路径（容器内路径），用于导入浏览器 Cookie。

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

- `v0.2.9`：参考 ChineseSubFinder/Bazarr 等项目的思路，新增 `subhd` 验证码冷却与 Cookie 注入能力（`SUBHD_CAPTCHA_COOLDOWN_SECONDS`、`SUBHD_COOKIE_STRING`、`SUBHD_COOKIE_FILE`），减少重复验证码失败。
- `v0.2.8`：优化自动下载重试策略：优先尝试非 subhd 候选；一旦识别到 subhd 验证码拦截，自动跳过其余 subhd 镜像候选并更快进入其他来源/回退链路。
- `v0.2.7`：进一步收紧剧集匹配规则：`tv` 查询新增标题重叠保护（弱季包候选需具备更可信标题相关性），降低“剧集格式正确但剧名不相关”的误命中。
- `v0.2.6`：新增“字幕内容级中文置信度校验”（基于对话行占比与中英字符占比），并强化 `movie/tv` 媒体类型强约束，解决电影/剧集候选混入问题。
- `v0.2.5`：参考 ChineseSubFinder 的候选过滤思路，新增标题词元重叠评分（含中文词元），并在电影请求中拒绝“中文标题零重叠”的误匹配候选，显著降低电影/剧集同名干扰。
- `v0.2.4`：Docker 镜像补齐 `bsdtar` 依赖（`libarchive-tools`），修复 RAR 字幕包在容器内无法解压导致的下载失败。
- `v0.2.3`：增强电影/剧集候选区分（电影请求会过滤明显剧集整季包），并在直连下载失败时明确提示已尝试 fallback 源但未命中可下载中文字幕。
- `v0.2.2`：新增“阶段内并发检索”（阶段间仍保序）；并发下继续保持下载失败自动换候选与跨阶段重试。
- `v0.2.1`：下载失败时自动在同阶段切换候选，仍失败则自动进入下一阶段 provider 重试（覆盖 `subliminal/direct` 混合场景）。
- `v0.2.0`：新增 `PROVIDER_STAGE_ORDER`、`MIN_SCORE`、`ALLOW_SEASON_PACK_FOR_EPISODE`，支持按用户偏好调整源优先级与匹配严格度（不再写死顺序）。
- `v0.1.9`：新增 `subhdtw` 直连源，并为 `subhd/subhdtw` 下载加入多镜像轮询重试（`subhd.tv/subhdtw.com/subhd.cc/subhd.me`）。
- `v0.1.8`：当 `assrt/subhd` 直连下载失败（含 `subhd` 验证码拦截）时，自动继续使用 fallback 下载链路（`podnapisi/tvsubtitles/opensubtitles`）。
- `v0.1.7`：`subhd` 升级为真实下载源（不再仅作提示词扩展），可直接下载并参与候选排序。
- `v0.1.6`：修复 `assrt` 网络/SSL 异常导致整次搜索失败的问题，失败时自动继续后续源与 fallback。
- `v0.1.5`：新增分层多源策略（`assrt/subhd` 后自动尝试 `podnapisi/tvsubtitles`，最后才用 OpenSubtitles）。
- `v0.1.4`：保留 IMDb/TMDB ID 参与检索，同时继续使用更严格的剧集候选过滤规则。
- `v0.1.3`：收紧剧集压缩包放宽规则，拒绝无集信息且标题不匹配的候选，进一步降低错剧/错集。
- `v0.1.2`：增强剧集字幕匹配与压缩包内目标集选择，降低错集字幕概率。
- `v0.1.1`：修复中文文件名在下载响应头中导致的 500 错误（`Content-Disposition` 编码兼容）。

## 测试

```bash
pytest -q
```
