"""
main.py — STAGE 1 of 2. The human approval gate.

One pass, then exit (cron invokes this; it is NOT a daemon):
    1. list new videos in the Dropbox watch folder (cursor-based delta)
    2. for each, read an optional sidecar .txt note, then generate metadata
       (the only LLM call)
    3. write a pending review file to state/pending/<id>.json
    4. STOP. main.py NEVER publishes.

You then open the pending file, edit the metadata if you like, set
"status": "approved", and run:  python publish.py <id>

Idempotency / safety:
    * Each video is de-duplicated by its stable Dropbox file id against existing
      pending AND processed records — a video is never turned into two pending
      files, even if the cursor is re-read.
    * The cursor is advanced ONLY when the run fully drains the new videos with
      zero failures and zero deferrals. Otherwise it is left untouched so the
      next run re-derives the same set (dedup keeps it from duplicating work).
      Net effect: a file is never silently lost and never double-queued.

Flags:
    --dry-run   do everything (incl. the LLM call so you see real output) EXCEPT
                writing pending files or advancing the cursor; logs what it WOULD
                write. (Note: --dry-run still calls Anthropic, which costs tokens.)

Exit code is non-zero on failure (fail loud).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set

import dropbox_client as dbxc
import publish
from config import Settings, load_config
from metadata import MetadataError, VideoContext, generate_metadata
from notify import notify, setup_logging

logger = logging.getLogger(__name__)


# --- Pending record helpers --------------------------------------------------
def _pending_dir(cfg: Settings) -> Path:
    return cfg.project_root / "state" / "pending"


def _processed_dir(cfg: Settings) -> Path:
    return cfg.project_root / "state" / "processed"


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "video"


def make_pending_id(video: dbxc.VideoFile) -> str:
    """Readable, stable, collision-resistant id: <slug>-<6 hex of file id>."""
    stem = Path(video.name).stem
    short = hashlib.sha1(video.id.encode("utf-8")).hexdigest()[:6]
    return f"{_slugify(stem)}-{short}"


def _load_seen_file_ids(cfg: Settings) -> Set[str]:
    """Dropbox file ids already turned into a pending OR processed record."""
    seen: Set[str] = set()
    for d in (_pending_dir(cfg), _processed_dir(cfg)):
        if not d.exists():
            continue
        for rec_path in d.glob("*.json"):
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
                fid = rec.get("dropbox", {}).get("file_id")
                if fid:
                    seen.add(fid)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping unreadable record %s: %s", rec_path, exc)
    return seen


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_pending_record(
    cfg: Settings, video: dbxc.VideoFile, pid: str, note: Optional[str], metadata
) -> dict:
    auto = cfg.run.auto_publish
    readme = (
        "AUTO-PUBLISH mode: this item is auto-approved and published immediately."
        if auto
        else (
            "Review and edit the metadata below. To APPROVE for publishing, set "
            f'"status" to "approved", save, then run:  python publish.py {pid}'
        )
    )
    return {
        "_README": readme,
        "id": pid,
        "status": "approved" if auto else "pending_review",
        "created_at": _now_iso(),
        "media_type": video.media_type,
        "dropbox": {
            "path": video.path_display,
            "path_lower": video.path_lower,
            "file_id": video.id,
            "content_hash": video.content_hash,
            "size": video.size,
        },
        "source_note": note,
        "youtube": {"privacy_status": cfg.youtube.privacy_status},
        "metadata": metadata.model_dump(),
    }


def write_skip_record(cfg: Settings, video: dbxc.VideoFile, pid: str) -> Path:
    """Record a dormant-media skip so the file is deduped and never reprocessed.

    Photos have no destination (Facebook was removed; YouTube has no photo API),
    so we neither generate metadata (no LLM cost) nor publish them — we note the
    skip in state/processed/ and leave the Dropbox source where it is.
    """
    record = {
        "id": pid,
        "status": "skipped",
        "created_at": _now_iso(),
        "media_type": video.media_type,
        "dropbox": {
            "path": video.path_display,
            "path_lower": video.path_lower,
            "file_id": video.id,
            "content_hash": video.content_hash,
            "size": video.size,
        },
        "reason": "photo posting is dormant (no destination configured)",
    }
    pdir = _processed_dir(cfg)
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{pid}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # atomic
    return path


def write_pending_record(cfg: Settings, pid: str, record: dict) -> Path:
    pdir = _pending_dir(cfg)
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{pid}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # atomic
    return path


# --- The run -----------------------------------------------------------------
def run(cfg: Settings, dry_run: bool) -> int:
    dbx = dbxc.build_client(cfg)
    account = dbxc.check_auth(dbx)
    logger.info("Authenticated to Dropbox as %s", account)

    listing = dbxc.list_new_videos(dbx, cfg)
    seen = _load_seen_file_ids(cfg)

    # Partition into work vs already-queued.
    fresh = [v for v in listing.videos if v.id not in seen]
    already = len(listing.videos) - len(fresh)

    cap = cfg.run.max_videos_per_run
    to_process = fresh[:cap]
    deferred = len(fresh) - len(to_process)

    written = 0
    published = 0
    failures = 0
    skipped = 0
    auto = cfg.run.auto_publish

    for video in to_process:
        try:
            # Dormant media: photos have no destination (Facebook removed;
            # YouTube has no photo API). Skip BEFORE the LLM call — record it so
            # it's deduped, leave the source in place, spend nothing.
            if video.media_type != "video":
                pid = make_pending_id(video)
                if dry_run:
                    logger.info("[DRY-RUN] would skip %s %s (no destination).",
                                video.media_type, video.name)
                    continue
                write_skip_record(cfg, video, pid)
                skipped += 1
                logger.info("Skipped %s %s (photo posting is dormant).",
                            video.media_type, video.name)
                continue

            note = dbxc.read_sidecar_note(dbx, video.path_lower)
            ctx = VideoContext(
                filename=video.name, size_bytes=video.size, notes=note,
                media_type=video.media_type,
            )
            metadata = generate_metadata(cfg, ctx)
            pid = make_pending_id(video)

            if dry_run:
                logger.info("[DRY-RUN] would write state/pending/%s.json", pid)
                logger.info("[DRY-RUN] title: %s", metadata.title)
                if auto:
                    logger.info("[DRY-RUN] would then auto-publish %s", pid)
                continue

            record = build_pending_record(cfg, video, pid, note, metadata)
            path = write_pending_record(cfg, pid, record)
            written += 1

            if auto:
                # Full-auto: publish immediately (publish.run re-validates, uploads
                # to both, moves the source, records the outcome, removes pending).
                logger.info("Auto-publishing %s", pid)
                rc = publish.run(cfg, pid, dry_run=False)
                if rc == 0:
                    published += 1
                else:
                    failures += 1  # publish.run already notified + moved to /failed
            else:
                logger.info("Wrote pending review file: %s", path)
        except MetadataError as exc:
            failures += 1
            notify(f"Metadata generation failed for {video.name}: {exc}", level="ERROR")
        except Exception as exc:  # any unexpected per-video error: don't lose the file
            failures += 1
            notify(f"Unexpected error processing {video.name}: {exc}", level="ERROR")
            logger.error("Traceback for %s", video.name, exc_info=True)

    # Advance the cursor ONLY on a fully clean drain (no failures, no deferrals).
    cursor_committed = False
    if not dry_run and failures == 0 and deferred == 0:
        dbxc.commit_cursor(cfg, listing.cursor)
        cursor_committed = True

    # One-line run summary to stdout.
    mode = "DRY-RUN " if dry_run else ""
    tail = (
        f"{published} published, {failures} failed"
        if auto
        else f"{written} pending written, {failures} failed"
    )
    summary = (
        f"{mode}main.py [{'auto' if auto else 'gated'}]: {len(listing.videos)} found, "
        f"{already} already-queued, {tail}, {skipped} skipped, {deferred} deferred; "
        f"cursor {'advanced' if cursor_committed else 'unchanged'}."
    )
    print(summary)
    logger.info(summary)

    if written and not dry_run and not auto:
        print(f"Review pending files in: {_pending_dir(cfg)}")

    return 1 if failures else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 1: find new videos, generate metadata, write pending review files.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate metadata and log what it WOULD write, without writing or advancing the cursor",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        setup_logging(cfg)
        return run(cfg, dry_run=args.dry_run)
    except Exception as exc:  # fail loud, non-zero exit
        logger.error("main.py failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
