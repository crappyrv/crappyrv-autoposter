"""
dropbox_client.py — all Dropbox I/O (deterministic, no LLM).

Capabilities:
  * build_client()      — auth via app key/secret + long-lived refresh token
  * list_new_videos()   — cursor-based delta listing of the watch folder,
                          filtered to video extensions; clean first-run behavior
  * download_file()     — download a Dropbox file to a local temp path
  * move_file()         — move a file between watch / posted / failed folders

Cursor model
------------
A Dropbox "cursor" is a bookmark into a folder's change stream.
  * First run (no cursor): we do a FULL listing of the watch folder and treat
    every existing video as new — so a video you just dropped in shows up. We
    compute the cursor at the end so the NEXT run only sees changes.
  * Later runs: we ask Dropbox only for changes since the saved cursor.
The cursor is stored in state/cursor.json alongside the folder it belongs to;
if the watch folder changes, the old cursor is ignored (treated as first run).

The fetch and the cursor-commit are deliberately SEPARATE. `list_new_videos()`
never advances the cursor by itself — the caller commits it only after the new
files have been safely handled (at-least-once; we never silently lose a file).
This also makes `--list` a safe, repeatable peek.

Manual test entry point:
    python dropbox_client.py --list                 # peek (does NOT advance cursor)
    python dropbox_client.py --list --commit        # peek AND advance the cursor
    python dropbox_client.py --reset-cursor         # forget the cursor (next run = first run)
    python dropbox_client.py --download "/incoming/clip.mp4"
    python dropbox_client.py --move "/incoming/clip.mp4" posted
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import List, Optional

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import DeletedMetadata, FileMetadata, FolderMetadata

from config import Settings, load_config
from notify import setup_logging

logger = logging.getLogger(__name__)


# --- Data structures ---------------------------------------------------------
@dataclass
class VideoFile:
    """A media file (video OR photo) discovered in the watch folder.

    The class name is historical — it now carries either kind, distinguished by
    `media_type` ("video" or "photo"). The downstream pipeline branches on it.
    """

    name: str
    path_lower: str           # canonical Dropbox path (use this for ops)
    path_display: str         # nicely-cased path for humans
    id: str                   # stable Dropbox file id (id:...)
    size: int                 # bytes
    server_modified: datetime
    content_hash: str
    rev: str
    media_type: str = "video"  # "video" | "photo"


@dataclass
class ListResult:
    """Result of a delta listing."""

    videos: List[VideoFile]
    cursor: str               # the NEW cursor (commit this after handling videos)
    first_run: bool           # True if there was no stored cursor for this folder


# --- Paths / cursor persistence ----------------------------------------------
def _cursor_path(cfg: Settings) -> Path:
    return cfg.project_root / "state" / "cursor.json"


def read_cursor(cfg: Settings) -> Optional[str]:
    """Return the stored cursor for the current watch folder, or None."""
    p = _cursor_path(cfg)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read cursor file (%s); treating as first run.", exc)
        return None
    if data.get("watch_folder") != cfg.dropbox.watch_folder:
        logger.info(
            "Stored cursor is for %r but watch folder is now %r; treating as first run.",
            data.get("watch_folder"),
            cfg.dropbox.watch_folder,
        )
        return None
    return data.get("cursor") or None


def commit_cursor(cfg: Settings, cursor: str) -> None:
    """Persist the cursor atomically, tagged with the folder it belongs to."""
    p = _cursor_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"watch_folder": cfg.dropbox.watch_folder, "cursor": cursor}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, p)  # atomic on POSIX
    logger.info("Cursor committed for %s.", cfg.dropbox.watch_folder)


def reset_cursor(cfg: Settings) -> bool:
    """Delete the stored cursor. Returns True if a cursor existed."""
    p = _cursor_path(cfg)
    if p.exists():
        p.unlink()
        logger.info("Cursor reset (deleted %s).", p)
        return True
    return False


# --- Auth --------------------------------------------------------------------
def build_client(cfg: Settings) -> dropbox.Dropbox:
    """
    Build an authenticated Dropbox client using the long-lived refresh token.
    The SDK transparently exchanges it for short-lived access tokens as needed.
    """
    return dropbox.Dropbox(
        oauth2_refresh_token=cfg.secrets.dropbox_refresh_token.get_secret_value(),
        app_key=cfg.secrets.dropbox_app_key.get_secret_value(),
        app_secret=cfg.secrets.dropbox_app_secret.get_secret_value(),
    )


def check_auth(dbx: dropbox.Dropbox) -> str:
    """Verify credentials; return the account's display name. Fail loud on auth error."""
    try:
        acct = dbx.users_get_current_account()
    except AuthError as exc:
        raise RuntimeError(
            "Dropbox authentication failed. Check DROPBOX_APP_KEY / "
            "DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN in .env (see CREDENTIALS.md)."
        ) from exc
    return acct.name.display_name


# --- Helpers -----------------------------------------------------------------
def _api_path(folder: str) -> str:
    """Dropbox uses '' for the root and '/Name' for folders."""
    if folder in ("", "/"):
        return ""
    return "/" + folder.strip("/")


def _is_video(name: str, cfg: Settings) -> bool:
    ext = Path(name).suffix.lower()
    return ext in cfg.dropbox.video_extensions


def _is_image(name: str, cfg: Settings) -> bool:
    ext = Path(name).suffix.lower()
    return ext in cfg.dropbox.image_extensions


def _media_type(name: str, cfg: Settings) -> Optional[str]:
    """Return "video", "photo", or None if the file is neither."""
    if _is_video(name, cfg):
        return "video"
    if _is_image(name, cfg):
        return "photo"
    return None


def _to_video(entry: FileMetadata, media_type: str = "video") -> VideoFile:
    return VideoFile(
        name=entry.name,
        path_lower=entry.path_lower,
        path_display=entry.path_display,
        id=entry.id,
        size=entry.size,
        server_modified=entry.server_modified,
        content_hash=entry.content_hash,
        rev=entry.rev,
        media_type=media_type,
    )


def human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{num} B"


# --- Listing -----------------------------------------------------------------
def _drain(dbx: dropbox.Dropbox, result) -> tuple[list, str]:
    """Page through has_more, collecting entries; return (entries, final_cursor)."""
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return entries, result.cursor


def list_new_videos(dbx: dropbox.Dropbox, cfg: Settings) -> ListResult:
    """
    Return new video files in the watch folder since the stored cursor.

    First run (no/invalid cursor): full listing of the folder; every existing
    video counts as new. Later runs: only changes since the cursor.

    Does NOT persist the cursor — the returned ListResult.cursor should be passed
    to commit_cursor() only after the caller has safely handled the videos.
    """
    api_path = _api_path(cfg.dropbox.watch_folder)
    stored = read_cursor(cfg)

    if stored is None:
        # First run: full listing.
        try:
            result = dbx.files_list_folder(api_path, recursive=False)
        except ApiError as exc:
            raise RuntimeError(
                f"Could not list Dropbox folder {cfg.dropbox.watch_folder!r}. "
                f"Does it exist? Create it in Dropbox (see CREDENTIALS.md). "
                f"Underlying error: {exc}"
            ) from exc
        entries, cursor = _drain(dbx, result)
        first_run = True
    else:
        # Delta listing. If the cursor is stale, Dropbox tells us to reset.
        try:
            result = dbx.files_list_folder_continue(stored)
        except ApiError as exc:
            is_reset = getattr(exc.error, "is_reset", lambda: False)()
            if is_reset:
                logger.warning("Cursor was invalidated by Dropbox; doing a full re-list.")
                reset_cursor(cfg)
                result = dbx.files_list_folder(api_path, recursive=False)
                entries, cursor = _drain(dbx, result)
                return ListResult(
                    videos=_filter_videos(entries, cfg), cursor=cursor, first_run=True
                )
            raise
        entries, cursor = _drain(dbx, result)
        first_run = False

    return ListResult(
        videos=_filter_videos(entries, cfg), cursor=cursor, first_run=first_run
    )


def _filter_videos(entries: list, cfg: Settings) -> List[VideoFile]:
    """Keep added/modified media FILES (videos AND photos); skip folders/deletions."""
    media: List[VideoFile] = []
    for entry in entries:
        if isinstance(entry, DeletedMetadata):
            continue  # a delete event (e.g. our own move-out); not new content
        if isinstance(entry, FolderMetadata):
            continue
        if isinstance(entry, FileMetadata):
            kind = _media_type(entry.name, cfg)
            if kind:
                media.append(_to_video(entry, kind))
    # Stable, predictable order: oldest first.
    media.sort(key=lambda v: v.server_modified)
    return media


# --- Download ----------------------------------------------------------------
def download_file(
    dbx: dropbox.Dropbox,
    cfg: Settings,
    dropbox_path: str,
    dest: Optional[Path] = None,
) -> Path:
    """
    Download a Dropbox file to a local path. If dest is None, write into the
    configured tmp dir using the source filename. Returns the local Path.
    """
    if dest is None:
        tmp_dir = cfg.project_root / cfg.run.tmp_dir
        tmp_dir.mkdir(parents=True, exist_ok=True)
        dest = tmp_dir / Path(dropbox_path).name
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Downloading %s -> %s", dropbox_path, dest)
    try:
        dbx.files_download_to_file(str(dest), dropbox_path)
    except ApiError as exc:
        raise RuntimeError(f"Failed to download {dropbox_path!r}: {exc}") from exc
    logger.info("Downloaded %s (%s).", dest.name, human_size(dest.stat().st_size))
    return dest


# --- Sidecar note ------------------------------------------------------------
def sidecar_path_for(video_path: str) -> str:
    """The .txt note path that sits next to a video (same name, .txt extension)."""
    return str(PurePosixPath(video_path).with_suffix(".txt"))


def read_sidecar_note(dbx: dropbox.Dropbox, video_path: str) -> Optional[str]:
    """
    Return the text of an optional sidecar note next to the video, or None.

    For 'Shower Doors - Too Heavy.mov' this looks for 'Shower Doors - Too
    Heavy.txt' in the same folder. Missing sidecar (or any download error) ->
    None; the note is purely optional.
    """
    sidecar = sidecar_path_for(video_path)
    try:
        _meta, resp = dbx.files_download(sidecar)
    except ApiError:
        logger.debug("No sidecar note at %s", sidecar)
        return None
    text = resp.content.decode("utf-8", errors="replace").strip()
    if text:
        logger.info("Found sidecar note: %s (%d chars)", sidecar, len(text))
    return text or None


# --- Move --------------------------------------------------------------------
def move_file(
    dbx: dropbox.Dropbox,
    from_path: str,
    dest_folder: str,
    *,
    autorename: bool = True,
) -> str:
    """
    Move a file into dest_folder (a Dropbox folder path). Returns the new path.
    autorename=True so a name clash never destroys/overwrites — Dropbox appends
    a suffix instead, and we log it.
    """
    name = Path(from_path).name
    folder = _api_path(dest_folder)
    to_path = f"{folder}/{name}" if folder else f"/{name}"
    logger.info("Moving %s -> %s", from_path, to_path)
    try:
        res = dbx.files_move_v2(from_path, to_path, autorename=autorename)
    except ApiError as exc:
        raise RuntimeError(
            f"Failed to move {from_path!r} -> {to_path!r}: {exc}"
        ) from exc
    new_path = res.metadata.path_display
    if new_path.lower() != to_path.lower():
        logger.warning("Destination existed; auto-renamed to %s", new_path)
    logger.info("Moved to %s", new_path)
    return new_path


# Convenience wrappers used later by main.py / publish.py.
def move_to_posted(dbx: dropbox.Dropbox, cfg: Settings, from_path: str) -> str:
    return move_file(dbx, from_path, cfg.dropbox.posted_folder)


def move_to_failed(dbx: dropbox.Dropbox, cfg: Settings, from_path: str) -> str:
    return move_file(dbx, from_path, cfg.dropbox.failed_folder)


# --- CLI / manual test entry point -------------------------------------------
def _cmd_list(dbx: dropbox.Dropbox, cfg: Settings, commit: bool) -> int:
    result = list_new_videos(dbx, cfg)
    mode = "FIRST RUN (full listing)" if result.first_run else "delta since last cursor"
    print(f"\nWatch folder : {cfg.dropbox.watch_folder}")
    print(f"Listing mode : {mode}")
    print(f"New videos   : {len(result.videos)}\n")

    if not result.videos:
        print("  (none) — drop a video into the watch folder and run --list again.")
    else:
        for v in result.videos:
            ts = v.server_modified.strftime("%Y-%m-%d %H:%M:%S")
            print(f"  • {v.name}")
            print(f"      path     : {v.path_display}")
            print(f"      size     : {human_size(v.size)}")
            print(f"      modified : {ts} UTC")

    if commit:
        commit_cursor(cfg, result.cursor)
        print("\nCursor COMMITTED — these files won't show as new again.")
    else:
        print("\nCursor NOT committed (peek only). Re-run with --commit to advance it.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dropbox client manual test entry point.")
    parser.add_argument("--list", action="store_true", help="list new videos in the watch folder")
    parser.add_argument("--commit", action="store_true", help="with --list, advance the stored cursor")
    parser.add_argument("--reset-cursor", action="store_true", help="forget the stored cursor")
    parser.add_argument("--download", metavar="DROPBOX_PATH", help="download a file to the tmp dir")
    parser.add_argument(
        "--move", nargs=2, metavar=("FROM_PATH", "DEST"),
        help="move a file; DEST is 'watch'|'posted'|'failed' or a folder path",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        setup_logging(cfg)

        # reset-cursor is standalone (no client needed).
        if args.reset_cursor:
            existed = reset_cursor(cfg)
            print("Cursor reset." if existed else "No cursor to reset.")
            if not (args.list or args.download or args.move):
                return 0

        dbx = build_client(cfg)
        account = check_auth(dbx)
        logger.info("Authenticated to Dropbox as %s", account)
        print(f"Authenticated as: {account}")

        if args.list:
            return _cmd_list(dbx, cfg, commit=args.commit)

        if args.download:
            dest = download_file(dbx, cfg, args.download)
            print(f"Downloaded to: {dest}")
            return 0

        if args.move:
            from_path, dest = args.move
            alias = {
                "watch": cfg.dropbox.watch_folder,
                "posted": cfg.dropbox.posted_folder,
                "failed": cfg.dropbox.failed_folder,
            }
            dest_folder = alias.get(dest, dest)
            new_path = move_file(dbx, from_path, dest_folder)
            print(f"Moved to: {new_path}")
            return 0

        parser.print_help()
        return 0
    except Exception as exc:  # fail loud, non-zero exit
        # One-line on console; full traceback in the rotating log file.
        logger.error("dropbox_client failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
