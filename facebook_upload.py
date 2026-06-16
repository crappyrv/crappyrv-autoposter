"""
facebook_upload.py — post a video to a Facebook Page via the Graph API.

Deterministic (no LLM). Uploads the video file to the Page's /videos edge with
the validated facebook_text as the description.

The Graph API version is pinned via GRAPH_API_VERSION and used in EVERY call —
we never rely on the API's default version. Keep this in sync with
config.yaml -> facebook.graph_api_version (a mismatch is logged as a warning).
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from config import Settings
from metadata import VideoMetadata

logger = logging.getLogger(__name__)

# Pinned Graph API version for EVERY Facebook call.
GRAPH_API_VERSION = "v25.0"
# Video uploads use the graph-video host.
GRAPH_VIDEO_BASE = f"https://graph-video.facebook.com/{GRAPH_API_VERSION}"

# Simple (single-request) upload is fine for typical clips. Larger files should
# move to the resumable/chunked protocol (TODO) — we guard with a size warning.
SIMPLE_UPLOAD_MAX_BYTES = 1_000_000_000  # ~1 GB
UPLOAD_TIMEOUT_SECONDS = 600


class FacebookUploadError(RuntimeError):
    """Raised when a Facebook upload fails."""


def upload_video(
    cfg: Settings,
    local_path: Path,
    metadata: VideoMetadata,
    scheduled_publish_time: int | None = None,
) -> dict:
    """
    Upload a video to the Page. Returns {"video_id", "post_id"?, "scheduled_publish_time"?}.
    Raises FacebookUploadError on failure.

    If scheduled_publish_time (a Unix timestamp) is given, the video is uploaded
    now but NOT published — Facebook publishes it automatically at that time
    (used to stagger the Page video behind the immediate Reel).
    """
    if cfg.facebook.graph_api_version != GRAPH_API_VERSION:
        logger.warning(
            "config graph_api_version (%s) != pinned GRAPH_API_VERSION (%s); "
            "using the pinned constant.",
            cfg.facebook.graph_api_version,
            GRAPH_API_VERSION,
        )

    size = local_path.stat().st_size
    if size > SIMPLE_UPLOAD_MAX_BYTES:
        raise FacebookUploadError(
            f"{local_path.name} is {size} bytes; exceeds simple-upload limit. "
            "Resumable upload not yet implemented."
        )

    page_id = cfg.secrets.facebook_page_id
    token = cfg.secrets.facebook_page_access_token.get_secret_value()
    url = f"{GRAPH_VIDEO_BASE}/{page_id}/videos"
    data = {
        "title": metadata.title,
        "description": metadata.facebook_text,
        "access_token": token,
    }
    if scheduled_publish_time:
        # Upload now, publish later: Facebook requires published=false + a Unix
        # timestamp 10 min – 6 months in the future.
        data["published"] = "false"
        data["scheduled_publish_time"] = str(int(scheduled_publish_time))
        logger.info(
            "Page video will be SCHEDULED for %s (unix)", int(scheduled_publish_time)
        )

    logger.info("Uploading to Facebook Page %s (%s)", page_id, GRAPH_API_VERSION)
    try:
        with local_path.open("rb") as fh:
            resp = requests.post(
                url, data=data, files={"source": fh}, timeout=UPLOAD_TIMEOUT_SECONDS
            )
    except requests.RequestException as exc:
        raise FacebookUploadError(f"Facebook request failed: {exc}") from exc

    if resp.status_code != 200:
        # Surface the Graph error but never the access token (it's in `data`, not
        # the response).
        raise FacebookUploadError(
            f"Facebook API returned {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    video_id = body.get("id")
    if not video_id:
        raise FacebookUploadError(f"no video id in response: {body}")
    when = "scheduled" if scheduled_publish_time else "published"
    logger.info("Facebook video upload complete (%s): video id %s", when, video_id)
    return {
        "video_id": video_id,
        "post_id": body.get("post_id"),
        "scheduled_publish_time": int(scheduled_publish_time) if scheduled_publish_time else None,
    }
