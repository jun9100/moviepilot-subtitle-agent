# MoviePilot Subtitle Agent

A FastAPI subtitle search/download service with MoviePilot-compatible API mode.

## Core Goal

This service is designed for Chinese users who already have Plex/Infuse OpenSubtitles integration but still miss Chinese subtitles there.

Default behavior:

- Focus on Chinese subtitles (`zh-cn`/`zh-tw`) only.
- Prefer non-OpenSubtitles sources (`assrt` + `subhd` hints).
- Keep OpenSubtitles providers as optional fallback instead of default path.

## Features

- Chinese subtitle search and direct download pipeline (Assrt as primary downloadable source).
- Uses SubHD as index hints to improve title/ID recall (especially when searching by IMDb/TMDB id).
- Supports movie and TV episode subtitle search.
- Supports subtitle download to disk and stream download.
- Includes MoviePilot-compatible search/download endpoints.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app.main:app --host 0.0.0.0 --port 8178
```

Health check:

```bash
curl http://127.0.0.1:8178/health
```

## Docker Quick Start

```bash
cp .env.example .env
docker compose up --build -d
curl http://127.0.0.1:8178/health
docker compose logs -f subtitle-agent
```

Stop:

```bash
docker compose down
```

## HTTPS Compatibility (macOS)

On macOS system Python builds that use LibreSSL (for example Python 3.9.6 + LibreSSL 2.8.3),
`urllib3` v2 may show TLS compatibility warnings and can cause unstable HTTPS behavior in
`requests`-based providers. This project pins `urllib3<2` on macOS in `requirements.txt` to
keep subtitle provider HTTPS calls stable.

If you use Python linked to OpenSSL 1.1.1+ (Homebrew/python.org/pyenv builds), this warning
does not apply.

## Standard APIs

- `POST /api/v1/subtitles/search`
- `POST /api/v1/subtitles/download`
- `GET /api/v1/subtitles/fetch/{token}`

## MoviePilot Compatibility APIs

- `POST /api/v1/moviepilot/subtitles/search`
- `GET /api/v1/moviepilot/subtitles/download/{token}`
- `POST /api/v1/moviepilot/subtitles/download`

Alias routes are also provided:

- `/api/moviepilot/...`
- `/moviepilot/...`

## MoviePilot Plugin (SubtitleAgentBridge)

Plugin example path:

`moviepilot-plugin-example/plugins.v2/subtitleagentbridge/__init__.py`

Install steps:

1. Copy the `subtitleagentbridge` directory into your MoviePilot `plugins.v2` directory.
2. Restart MoviePilot and enable `Subtitle Agent Bridge`.
3. Configure:
   - `host`: Subtitle Agent URL (for Dockerized MoviePilot, usually `http://host.docker.internal:8178`)
   - `search_path`: `/api/v1/moviepilot/subtitles/search`
   - `languages`: `zh-cn,zh-tw`
4. Trigger a media transfer/import in MoviePilot. The plugin listens to `TransferComplete` and will auto search+download subtitles.

## Tests

```bash
pytest -q
```
