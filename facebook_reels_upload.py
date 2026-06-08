"""
facebook_reels_upload.py — publish a Facebook Page REEL via the Graph API.

Reels use a different, 3-step flow than the regular /videos edge:
  1. start  -> POST /{page_id}/video_reels?upload_phase=start  (returns video_id + upload_url)
  2. upload -> POST the raw file bytes to the returned rupload URL
  3. finish -> POST /{page_id}/video_reels?upload_phase=finish&video_state=PUBLISHED

Same pinned Graph API version + same Page permissions as facebook_upload (no new
audit/approval). Reels must be VERTICAL (9:16) and ~3-90s; non-conforming videos
are rejected by Facebook at the finish/processing step.
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
UPLOAD_TIMEOUT_SECONDS = 600


class FacebookReelError(RuntimeError):
    """Raised when a Facebook Reel upload fails."""


def upload_reel(cfg: Settings, local_path: Path, metadata: VideoMetadata) -> dict:
    """Publish a Page Reel. Returns {"video_id"}. Raises FacebookReelError."""
    page_id = cfg.secrets.facebook_page_id
    token = cfg.secrets.facebook_page_access_token.get_secret_value()
    endpoint = f"{GRAPH_BASE}/{page_id}/video_reels"

    # 1) start an upload session
    try:
        r = requests.post(
            endpoint, data={"upload_phase": "start", "access_token": token}, timeout=60
        )
    except requests.RequestException as exc:
        raise FacebookReelError(f"Reel start request failed: {exc}") from exc
    if r.status_code != 200:
        raise FacebookReelError(f"Reel start returned {r.status_code}: {r.text[:400]}")
    j = r.json()
    video_id, upload_url = j.get("video_id"), j.get("upload_url")
    if not video_id or not upload_url:
        raise FacebookReelError(f"Reel start missing video_id/upload_url: {j}")

    # 2) upload the raw bytes to the rupload URL
    size = local_path.stat().st_size
    logger.info("Uploading Reel bytes to Facebook (%s, %d bytes)", GRAPH_API_VERSION, size)
    try:
        with local_path.open("rb") as fh:
            up = requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(size),
                },
                data=fh,
                timeout=UPLOAD_TIMEOUT_SECONDS,
            )
    except requests.RequestException as exc:
        raise FacebookReelError(f"Reel byte upload failed: {exc}") from exc
    if up.status_code != 200:
        raise FacebookReelError(f"Reel byte upload returned {up.status_code}: {up.text[:400]}")

    # 3) publish
    try:
        fin = requests.post(
            endpoint,
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": metadata.facebook_text,
                "access_token": token,
            },
            timeout=120,
        )
    except requests.RequestException as exc:
        raise FacebookReelError(f"Reel finish request failed: {exc}") from exc
    if fin.status_code != 200:
        raise FacebookReelError(f"Reel finish returned {fin.status_code}: {fin.text[:400]}")
    body = fin.json()
    if body.get("success") is False:
        raise FacebookReelError(f"Reel finish reported failure: {body}")

    # Note: FB still encodes the reel asynchronously after 'finish'; a non-conforming
    # (non-vertical/too-long) video can still fail during processing.
    logger.info("Facebook Reel published (encoding async): video id %s", video_id)
    return {"video_id": video_id}
