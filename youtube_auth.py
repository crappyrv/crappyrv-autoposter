"""
youtube_auth.py — one-time helper to mint a YouTube OAuth refresh token.

Prereqs (see CREDENTIALS.md §3):
  * YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET set in .env (Desktop OAuth client)
  * YouTube Data API v3 enabled on the Google Cloud project
  * OAuth consent screen PUBLISHED to "In production" (else the refresh token
    expires after 7 days)

Run it once, interactively:

    python youtube_auth.py

It opens your browser via a localhost loopback, you sign in with the channel's
Google account and approve, and it writes YOUTUBE_REFRESH_TOKEN into .env. The
token value is never printed.

If you see an "unverified app" warning, that's expected for a single-user app you
own: click Advanced -> "Go to ... (unsafe)" to continue.
"""

from __future__ import annotations

import sys

from google_auth_oauthlib.flow import InstalledAppFlow

from config import load_config, update_env_var

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def main() -> int:
    cfg = load_config()
    client_config = {
        "installed": {
            "client_id": cfg.secrets.youtube_client_id.get_secret_value(),
            "client_secret": cfg.secrets.youtube_client_secret.get_secret_value(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(
        client_config, scopes=[YOUTUBE_UPLOAD_SCOPE]
    )

    print("\nA browser window will open for Google sign-in + consent.")
    print("Use the Google account that owns the YouTube channel.\n")
    try:
        # access_type=offline + prompt=consent guarantees a refresh token.
        creds = flow.run_local_server(
            port=0, access_type="offline", prompt="consent"
        )
    except Exception as exc:  # fail loud
        print(f"\nERROR during OAuth flow: {exc}", file=sys.stderr)
        return 1

    if not creds.refresh_token:
        print(
            "\nERROR: no refresh token returned. Re-run; if it persists, revoke "
            "the app's access in your Google account and try again.",
            file=sys.stderr,
        )
        return 1

    update_env_var("YOUTUBE_REFRESH_TOKEN", creds.refresh_token)
    print("\n✅ Success. YOUTUBE_REFRESH_TOKEN written to .env (value not shown).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
