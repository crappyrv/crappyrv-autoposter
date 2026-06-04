"""
publish.py — STAGE 2 of 2. Runs only on an APPROVED pending item.

    python publish.py <pending-id>
    python publish.py <pending-id> --dry-run

Steps:
    1. load state/pending/<id>.json; require "status": "approved"
    2. re-validate the (possibly human-edited) metadata — never publish invalid
    3. download the source video from Dropbox
    4. upload to YouTube AND Facebook
    5. move the Dropbox source to /posted ONLY if BOTH uploads succeed;
       otherwise move it to /failed and notify()
    6. record the outcome to state/processed/<id>.json and remove the pending file

Failure semantics (deliberate, per spec):
    * approval/validation problems  -> stop, leave everything as-is so you can fix
      and retry (NOT moved to /failed; nothing was published)
    * upload-phase failure          -> move source to /failed, write a processed
      record, remove pending, notify. (No auto-retry — a human decides.) If
      YouTube succeeded but Facebook failed, the processed record + notification
      say so explicitly (the YouTube video exists, unlisted by default).
    * We always write a processed record and remove the pending file after an
      upload ATTEMPT, so a video is never re-published by a later run.

--dry-run does read-only checks (pending approved, metadata valid, source exists)
and logs exactly what it WOULD upload and move — no uploads, no moves, no writes.

Exit code is non-zero on failure (fail loud).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import dropbox_client as dbxc
import facebook_upload
import youtube_upload
from config import Settings, load_config
from metadata import MetadataError, validate_metadata
from notify import notify, setup_logging

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pending_path(cfg: Settings, pid: str) -> Path:
    return cfg.project_root / "state" / "pending" / f"{pid}.json"


def _processed_path(cfg: Settings, pid: str) -> Path:
    return cfg.project_root / "state" / "processed" / f"{pid}.json"


def _write_processed(cfg: Settings, pid: str, record: dict) -> Path:
    path = _processed_path(cfg, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


# --- The publish run ---------------------------------------------------------
def run(cfg: Settings, pid: str, dry_run: bool) -> int:
    pending_path = _pending_path(cfg, pid)
    if not pending_path.exists():
        raise RuntimeError(
            f"No pending item {pid!r} at {pending_path}. "
            f"Already published? Check state/processed/."
        )

    record = json.loads(pending_path.read_text(encoding="utf-8"))

    # --- Gate 1: approval ---
    status = record.get("status")
    if status != "approved":
        raise RuntimeError(
            f"Pending item {pid!r} is not approved (status={status!r}). "
            f'Edit {pending_path}, set "status": "approved", then re-run.'
        )

    # --- Gate 2: metadata still valid (human may have edited it) ---
    try:
        meta = validate_metadata(cfg, record.get("metadata", {}))
    except MetadataError as exc:
        raise RuntimeError(
            f"Refusing to publish {pid!r}: {exc}. Fix the metadata in "
            f"{pending_path} and re-run."
        ) from exc

    dbx_info = record.get("dropbox", {})
    src_path = dbx_info.get("path_lower") or dbx_info.get("path")
    if not src_path:
        raise RuntimeError(f"Pending item {pid!r} has no Dropbox source path.")

    privacy = record.get("youtube", {}).get("privacy_status", cfg.youtube.privacy_status)
    dbx = dbxc.build_client(cfg)

    # --- Dry run: read-only preview, no side effects ---
    if dry_run:
        try:
            dbx.files_get_metadata(src_path)
            exists = True
        except Exception as exc:  # noqa: BLE001 - report, don't crash the preview
            exists = False
            logger.warning("[DRY-RUN] source not found at %s: %s", src_path, exc)
        logger.info("[DRY-RUN] pending %s is approved and metadata is valid.", pid)
        logger.info("[DRY-RUN] source exists: %s (%s)", exists, src_path)
        logger.info("[DRY-RUN] WOULD upload to YouTube: title=%r privacy=%s",
                    meta.title, privacy)
        logger.info("[DRY-RUN] WOULD upload to Facebook: caption=%r", meta.facebook_text)
        target = cfg.dropbox.posted_folder
        logger.info("[DRY-RUN] WOULD move source -> %s on success (else %s)",
                    target, cfg.dropbox.failed_folder)
        summary = f"DRY-RUN publish.py: {pid} ready to publish (no actions taken)."
        print(summary)
        logger.info(summary)
        return 0

    # --- Real publish ---
    results = {"youtube": {"ok": False}, "facebook": {"ok": False}}
    local_path: Optional[Path] = None
    upload_error: Optional[str] = None

    try:
        local_path = dbxc.download_file(dbx, cfg, src_path)

        # YouTube first.
        yt = youtube_upload.upload_video(cfg, local_path, meta, privacy)
        results["youtube"] = {"ok": True, **yt}

        # Then Facebook.
        fb = facebook_upload.upload_video(cfg, local_path, meta)
        results["facebook"] = {"ok": True, **fb}

    except Exception as exc:  # any upload-phase failure
        upload_error = str(exc)
        logger.error("Publish failed for %s: %s", pid, exc, exc_info=True)
    finally:
        if local_path and local_path.exists():
            local_path.unlink()  # clean up the downloaded temp file

    both_ok = results["youtube"]["ok"] and results["facebook"]["ok"]

    # --- Move the source: /posted only if BOTH succeeded, else /failed ---
    final_path = None
    move_error = None
    try:
        if both_ok:
            final_path = dbxc.move_to_posted(dbx, cfg, src_path)
        else:
            final_path = dbxc.move_to_failed(dbx, cfg, src_path)
    except Exception as exc:  # noqa: BLE001
        move_error = str(exc)
        logger.error("Failed to move source for %s: %s", pid, exc, exc_info=True)

    # --- Record the outcome + remove the pending file (no double-publish ever) ---
    processed = {
        "id": pid,
        "status": "posted" if both_ok else "failed",
        "published_at": _now_iso(),
        "dropbox": {**dbx_info, "final_path": final_path, "move_error": move_error},
        "results": results,
        "upload_error": upload_error,
        "metadata": meta.model_dump(),
    }
    _write_processed(cfg, pid, processed)
    pending_path.unlink(missing_ok=True)

    # --- Notify + summarize ---
    if both_ok:
        msg = (
            f"Published {pid}: YouTube {results['youtube']['url']} "
            f"(privacy={privacy}), Facebook id {results['facebook']['video_id']}. "
            f"Source moved to {final_path}."
        )
        notify(msg, level="INFO")
    else:
        parts = []
        if results["youtube"]["ok"]:
            parts.append(
                f"YouTube SUCCEEDED ({results['youtube']['url']}, privacy={privacy}) "
                "— that video exists; delete or adjust it manually if needed"
            )
        else:
            parts.append("YouTube failed")
        parts.append("Facebook succeeded" if results["facebook"]["ok"] else "Facebook failed")
        notify(
            f"Publish FAILED for {pid}: {'; '.join(parts)}. "
            f"Error: {upload_error}. Source moved to {final_path or '(move failed!)'}.",
            level="ERROR",
        )
    if move_error:
        notify(
            f"WARNING: source for {pid} could not be moved ({move_error}); "
            f"it may still be in the watch folder. Manual cleanup needed.",
            level="ERROR",
        )

    summary = (
        f"publish.py: {pid} -> {'POSTED' if both_ok else 'FAILED'}; "
        f"youtube={'ok' if results['youtube']['ok'] else 'fail'}, "
        f"facebook={'ok' if results['facebook']['ok'] else 'fail'}; "
        f"source -> {final_path or 'NOT MOVED'}."
    )
    print(summary)
    logger.info(summary)
    return 0 if both_ok else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2: publish an approved pending item.")
    parser.add_argument("pending_id", help="the pending id (filename without .json)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="read-only preview; no uploads, no moves, no writes",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        setup_logging(cfg)
        return run(cfg, args.pending_id, dry_run=args.dry_run)
    except Exception as exc:  # fail loud, non-zero exit
        logger.error("publish.py failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
