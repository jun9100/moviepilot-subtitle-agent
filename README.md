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

## Tests

```bash
pytest -q
```
