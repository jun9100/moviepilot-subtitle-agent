from __future__ import annotations

import io

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings, get_settings
from .errors import SubtitleError
from .models import (
    DownloadRequest,
    DownloadResponse,
    MoviePilotEnvelope,
    MoviePilotSearchRequest,
    MoviePilotSubtitleItem,
    SearchRequest,
    SearchResponse,
)
from .service import SubtitleService


def _moviepilot_error(message: str) -> MoviePilotEnvelope:
    return MoviePilotEnvelope(success=False, message=message, data=None)


def create_app(
    *,
    settings: Settings | None = None,
    service: SubtitleService | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()

    app = FastAPI(
        title=app_settings.app_name,
        version=app_settings.app_version,
    )
    app.state.settings = app_settings
    app.state.subtitle_service = service or SubtitleService(settings=app_settings)

    @app.exception_handler(SubtitleError)
    async def subtitle_error_handler(_: Request, exc: SubtitleError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    def get_service(request: Request) -> SubtitleService:
        return request.app.state.subtitle_service

    @app.get("/health")
    @app.get("/api/health")
    @app.get("/api/v1/health")
    def health(request: Request) -> dict[str, object]:
        settings_state: Settings = request.app.state.settings
        return {
            "status": "ok",
            "name": settings_state.app_name,
            "version": settings_state.app_version,
            "providers": settings_state.provider_list,
            "default_languages": settings_state.language_list,
        }

    @app.post("/api/v1/subtitles/search", response_model=SearchResponse)
    def search_subtitles(payload: SearchRequest, request: Request) -> SearchResponse:
        return get_service(request).search(payload)

    @app.post("/api/v1/subtitles/download", response_model=DownloadResponse)
    def download_subtitle(payload: DownloadRequest, request: Request) -> DownloadResponse:
        return get_service(request).download_to_disk(payload.token, filename=payload.filename)

    @app.get("/api/v1/subtitles/fetch/{token}")
    def fetch_subtitle(token: str, request: Request):
        fetched = get_service(request).fetch_to_memory(token)
        media_type = "application/x-subrip" if fetched.subtitle_format == "srt" else "application/octet-stream"
        headers = {
            "Content-Disposition": f'attachment; filename="{fetched.filename}"',
        }
        return StreamingResponse(io.BytesIO(fetched.content), media_type=media_type, headers=headers)

    @app.post("/api/v1/moviepilot/subtitles/search", response_model=MoviePilotEnvelope)
    @app.post("/api/moviepilot/subtitles/search", response_model=MoviePilotEnvelope)
    @app.post("/moviepilot/subtitles/search", response_model=MoviePilotEnvelope)
    def moviepilot_search(payload: MoviePilotSearchRequest, request: Request) -> MoviePilotEnvelope:
        settings_state: Settings = request.app.state.settings
        query = payload.to_search_request(default_languages=settings_state.language_list)

        try:
            response = get_service(request).search(query)
        except SubtitleError as exc:
            return _moviepilot_error(exc.message)

        items = [
            MoviePilotSubtitleItem(
                id=item.token,
                provider=item.provider,
                subtitle_id=item.subtitle_id,
                name=item.title,
                language=item.language,
                score=item.score,
                format=item.subtitle_format,
                hearing_impaired=item.hearing_impaired,
                page_link=item.page_link,
                matches=item.matches,
                download_url=f"/api/v1/moviepilot/subtitles/download/{item.token}",
            ).model_dump()
            for item in response.items
        ]

        return MoviePilotEnvelope(
            success=True,
            message="ok",
            data={
                "query": response.query.model_dump(),
                "providers": response.providers,
                "total": response.total,
                "items": items,
            },
        )

    @app.get("/api/v1/moviepilot/subtitles/download/{token}")
    @app.get("/api/moviepilot/subtitles/download/{token}")
    @app.get("/moviepilot/subtitles/download/{token}")
    def moviepilot_download(token: str, request: Request):
        try:
            fetched = get_service(request).fetch_to_memory(token)
        except SubtitleError as exc:
            return JSONResponse(status_code=200, content=_moviepilot_error(exc.message).model_dump())

        media_type = "application/x-subrip" if fetched.subtitle_format == "srt" else "application/octet-stream"
        headers = {
            "Content-Disposition": f'attachment; filename="{fetched.filename}"',
        }
        return StreamingResponse(io.BytesIO(fetched.content), media_type=media_type, headers=headers)

    @app.post("/api/v1/moviepilot/subtitles/download", response_model=MoviePilotEnvelope)
    @app.post("/api/moviepilot/subtitles/download", response_model=MoviePilotEnvelope)
    @app.post("/moviepilot/subtitles/download", response_model=MoviePilotEnvelope)
    def moviepilot_download_to_disk(payload: DownloadRequest, request: Request) -> MoviePilotEnvelope:
        try:
            downloaded = get_service(request).download_to_disk(payload.token, filename=payload.filename)
        except SubtitleError as exc:
            return _moviepilot_error(exc.message)

        return MoviePilotEnvelope(success=True, message="ok", data=downloaded.model_dump())

    return app


app = create_app()
