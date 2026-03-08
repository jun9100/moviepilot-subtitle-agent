from __future__ import annotations


class SubtitleError(Exception):
    status_code = 400

    def __init__(self, message: str, *, data: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data


class SubtitleNotFoundError(SubtitleError):
    status_code = 404


class SubtitleDownloadError(SubtitleError):
    status_code = 502


class SubtitleCaptchaError(SubtitleDownloadError):
    status_code = 409


class SubtitleSearchError(SubtitleError):
    status_code = 502
