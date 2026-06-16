"""
facebook_photo_upload.py — post a photo to a Facebook Page via the Graph API.

Deterministic (no LLM). Uploads the image file to the Page's /photos edge with
the validated facebook_text as the caption (`message`). One request.

Same pinned Graph API version + same Page permissions (pages_manage_posts) as
facebook_upload — posting a photo needs no extra audit/approval beyond what the
Page video posting already uses.

YouTube is intentionally NOT a target for photos: the YouTube Data API has no
photo-upload endpoint, and there is no "photo reel" on Facebook. A dropped image
posts to the Page photo feed only.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from config import Settings
from facebook_upload import GRAPH_API_VERSION  # reuse the single pinned version
from metadata import VideoMetadata

logger = logging.getLogger(__name__)

GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
UPLOAD_TIMEOUT_SECONDS = 300


class FacebookPhotoError(RuntimeError):
    """Raised when a Facebook photo upload fails."""


def upload_photo(cfg: Settings, local_path: Path, metadata: VideoMetadata) -> dict:
    """
    Upload a photo to the Page. Returns {"photo_id", "post_id"}.
    Raises FacebookPhotoError on failure.
    """
    page_id = cfg.secrets.facebook_page_id
    token = cfg.secrets.facebook_page_access_token.get_secret_value()
    url = f"{GRAPH_BASE}/{page_id}/photos"
    data = {
        # /photos uses `caption` for the text shown with the photo.
        "caption": metadata.facebook_text,
        "published": "true",
        "access_token": token,
    }

    logger.info("Uploading photo to Facebook Page %s (%s)", page_id, GRAPH_API_VERSION)
    try:
        with local_path.open("rb") as fh:
            resp = requests.post(
                url, data=data, files={"source": fh}, timeout=UPLOAD_TIMEOUT_SECONDS
            )
    except requests.RequestException as exc:
        raise FacebookPhotoError(f"Facebook photo request failed: {exc}") from exc

    if resp.status_code != 200:
        # Surface the Graph error but never the access token (it's in `data`).
        raise FacebookPhotoError(
            f"Facebook API returned {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    photo_id = body.get("id")
    if not photo_id:
        raise FacebookPhotoError(f"no photo id in response: {body}")
    logger.info("Facebook photo upload complete: photo id %s", photo_id)
    return {"photo_id": photo_id, "post_id": body.get("post_id")}
