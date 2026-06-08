"""
youtube_upload.py — resumable upload to YouTube using the saved refresh token.

Deterministic (no LLM). Builds a YouTube Data API v3 client from the stored
refresh token (no browser) and performs a resumable media upload with the
validated metadata. privacyStatus comes from the pending record / config and
defaults to "unlisted".
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from config import Settings
from metadata import VideoMetadata
from youtube_auth import YOUTUBE_UPLOAD_SCOPE

logger = logging.getLogger(__name__)


class YouTubeUploadError(RuntimeError):
    """Raised when a YouTube upload fails."""


def build_youtube(cfg: Settings):
    """Build a YouTube Data API client from the refresh token (no browser)."""
    creds = Credentials(
        token=None,
        refresh_token=cfg.secrets.youtube_refresh_token.get_secret_value(),
        client_id=cfg.secrets.youtube_client_id.get_secret_value(),
        client_secret=cfg.secrets.youtube_client_secret.get_secret_value(),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[YOUTUBE_UPLOAD_SCOPE],
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_video(
    cfg: Settings,
    local_path: Path,
    metadata: VideoMetadata,
    privacy_status: str,
) -> dict:
    """
    Upload a video resumably. Returns {"video_id", "url", "privacy_status"}.
    Raises YouTubeUploadError on failure.
    """
    if privacy_status not in {"private", "unlisted", "public"}:
        raise YouTubeUploadError(f"invalid privacy_status: {privacy_status!r}")

    youtube = build_youtube(cfg)

    # Nudge YouTube to classify vertical, <=3min clips as Shorts.
    description = metadata.description
    if cfg.youtube.add_shorts_hashtag and "#shorts" not in description.lower():
        description = (description.rstrip() + "\n\n#Shorts")[:YT_DESCRIPTION_MAX]

    body = {
        "snippet": {
            "title": metadata.title,
            "description": description,
            "tags": metadata.tags,
            "categoryId": cfg.youtube.category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": cfg.youtube.made_for_kids,
        },
    }

    mimetype = mimetypes.guess_type(str(local_path))[0] or "video/mp4"
    media = MediaFileUpload(str(local_path), mimetype=mimetype, resumable=True)

    logger.info(
        "Uploading to YouTube (privacy=%s): %s", privacy_status, metadata.title
    )
    try:
        request = youtube.videos().insert(
            part="snippet,status", body=body, media_body=media
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info("YouTube upload %d%%", int(status.progress() * 100))
    except HttpError as exc:
        raise YouTubeUploadError(f"YouTube API error: {exc}") from exc
    except Exception as exc:  # network etc.
        raise YouTubeUploadError(f"YouTube upload failed: {exc}") from exc

    video_id = response.get("id")
    if not video_id:
        raise YouTubeUploadError(f"no video id in response: {response}")
    url = f"https://youtu.be/{video_id}"
    logger.info("YouTube upload complete: %s", url)
    return {"video_id": video_id, "url": url, "privacy_status": privacy_status}
