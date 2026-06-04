"""
dropbox_auth.py — one-time helper to mint a long-lived Dropbox REFRESH token.

Prereqs (already done if you reached this point):
  * DROPBOX_APP_KEY and DROPBOX_APP_SECRET are set in .env
  * On the app's Permissions tab these scopes are CHECKED and submitted:
      files.metadata.read, files.content.read, files.content.write
    (account_info.read is on by default and lets us show "Authenticated as ...")

Run it once, interactively:

    python dropbox_auth.py

It prints an authorize URL, you click Allow in the browser and paste back the
short code, and it writes DROPBOX_REFRESH_TOKEN into .env for you. The token
value is never printed to the screen or the logs.

Note on scopes: we intentionally do NOT request an explicit scope list here, so
the refresh token inherits exactly the scopes you enabled on the app's
Permissions tab. That avoids a "requested a scope the app doesn't have" error.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from dropbox import DropboxOAuth2FlowNoRedirect

from config import ENV_PATH, load_config


def _write_refresh_token(env_path: Path, token: str) -> None:
    """Replace (or append) the DROPBOX_REFRESH_TOKEN line in .env, atomically."""
    line = f"DROPBOX_REFRESH_TOKEN={token}"
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        if re.search(r"^DROPBOX_REFRESH_TOKEN=.*$", text, flags=re.MULTILINE):
            text = re.sub(
                r"^DROPBOX_REFRESH_TOKEN=.*$", line, text, flags=re.MULTILINE
            )
        else:
            text = text.rstrip("\n") + "\n" + line + "\n"
    else:
        text = line + "\n"
    tmp = env_path.with_suffix(".env.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(env_path)


def main() -> int:
    cfg = load_config()
    app_key = cfg.secrets.dropbox_app_key.get_secret_value()
    app_secret = cfg.secrets.dropbox_app_secret.get_secret_value()

    flow = DropboxOAuth2FlowNoRedirect(
        app_key,
        app_secret,
        token_access_type="offline",  # <-- this is what yields a refresh token
    )

    authorize_url = flow.start()
    print("\n1. Open this URL in your browser and click 'Allow':\n")
    print("   " + authorize_url + "\n")
    print("2. Copy the authorization code Dropbox shows you.\n")

    try:
        code = input("3. Paste the code here and press Enter: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 1

    if not code:
        print("No code entered. Aborting.", file=sys.stderr)
        return 1

    try:
        result = flow.finish(code)
    except Exception as exc:  # fail loud
        print(f"\nERROR exchanging the code: {exc}", file=sys.stderr)
        print("Double-check the code and that the three scopes are enabled.", file=sys.stderr)
        return 1

    if not getattr(result, "refresh_token", None):
        print(
            "\nERROR: Dropbox did not return a refresh token. Make sure the app "
            "uses 'offline' access (this script requests it).",
            file=sys.stderr,
        )
        return 1

    _write_refresh_token(ENV_PATH, result.refresh_token)
    print("\n✅ Success. DROPBOX_REFRESH_TOKEN written to .env (value not shown).")
    print("   Next:  python dropbox_client.py --list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
