"""
dropbox_client — the merch pipeline's view of Dropbox.

Design: no cursors. A file that is still sitting in a drop folder (Shirts/Mugs/
Garden Flags) is by definition unprocessed. On success we move it to _done, on
failure to _failed, so the drop folders only ever hold new work. Idempotent and
crash-safe: a half-finished run just leaves the file in place for the next pass.
"""
from __future__ import annotations
import time
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

import dropbox
from dropbox.files import WriteMode


# Don't grab a file that's still syncing from the desktop.
_SETTLE_SECONDS = 90


def _retry(fn, attempts: int = 5, backoff=(2, 4, 8, 16)):
    """Retry a Dropbox network op on transient errors so a blip never fails a run."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except (dropbox.exceptions.InternalServerError,
                dropbox.exceptions.RateLimitError,
                ConnectionError, TimeoutError) as e:
            last = e
            if i < attempts - 1:
                time.sleep(backoff[i])
    raise last


@dataclass
class Incoming:
    product_type: str      # e.g. "shirts"
    path: str              # full Dropbox path
    name: str              # file name
    modified_epoch: float


def get_client(secrets) -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=secrets.dropbox_refresh_token,
        app_key=secrets.dropbox_app_key,
        app_secret=secrets.dropbox_app_secret,
    )


def list_incoming(dbx: dropbox.Dropbox, settings) -> list[Incoming]:
    out: list[Incoming] = []
    exts = settings.image_extensions
    now = time.time()
    for ptype, cfg in settings.product_types.items():
        folder = f"{settings.drop_root}/{cfg['folder']}"
        try:
            res = dbx.files_list_folder(folder)
        except dropbox.exceptions.ApiError:
            continue  # folder missing — skip silently
        entries = list(res.entries)
        while res.has_more:
            res = dbx.files_list_folder_continue(res.cursor)
            entries.extend(res.entries)
        for e in entries:
            if not isinstance(e, dropbox.files.FileMetadata):
                continue
            if Path(e.name).suffix.lower() not in exts:
                continue
            # Dropbox server_modified is naive UTC; make it tz-aware before epoch
            # conversion or local-timezone machines misjudge the file's age.
            mod = e.server_modified.replace(tzinfo=timezone.utc).timestamp()
            if now - mod < _SETTLE_SECONDS:
                continue  # still settling
            out.append(Incoming(ptype, e.path_lower, e.name, mod))
    return out


def download(dbx: dropbox.Dropbox, path: str, dest: str | Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    md, resp = _retry(lambda: dbx.files_download(path))
    dest.write_bytes(resp.content)
    return dest


def upload_printfile(dbx: dropbox.Dropbox, local_path: str | Path, folder: str, name: str) -> str:
    """Upload a processed print file and return a temporary direct URL (~4h)
    that Printify can fetch."""
    local_path = Path(local_path)
    dest = f"{folder}/{name}"
    data = Path(local_path).read_bytes()
    _retry(lambda: dbx.files_upload(data, dest, mode=WriteMode.overwrite, mute=True))
    link = _retry(lambda: dbx.files_get_temporary_link(dest))
    return link.link


def move_out(dbx: dropbox.Dropbox, src_path: str, dest_folder: str, product_type: str, name: str) -> str:
    """Move a processed source file to _done/<type>/ or _failed/<type>/."""
    dest = f"{dest_folder}/{product_type}/{name}"
    try:
        dbx.files_create_folder_v2(f"{dest_folder}/{product_type}")
    except dropbox.exceptions.ApiError:
        pass
    try:
        dbx.files_move_v2(src_path, dest, autorename=True)
    except dropbox.exceptions.ApiError:
        # If a same-named file exists, autorename handles it; anything else, re-raise-ish.
        dbx.files_move_v2(src_path, dest, autorename=True)
    return dest
