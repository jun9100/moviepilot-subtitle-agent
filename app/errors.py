from __future__ import annotations


class SubtitleError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SubtitleNotFoundError(SubtitleError):
    status_code = 404


class SubtitleDownloadError(SubtitleError):
    status_code = 502


class SubtitleSearchError(SubtitleError):
    status_code = 502
