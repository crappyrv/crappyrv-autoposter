"""
notify — free phone push via ntfy.sh (no account, no cost).

David installs the ntfy app (iOS/Android), subscribes to the secret topic in
NTFY_TOPIC, and gets a push the moment a new Etsy draft is ready — tap it to jump
straight to the listing. If NTFY_TOPIC is unset, notifications degrade to stdout.
"""
from __future__ import annotations
import requests


def push(topic: str, title: str, message: str,
         click_url: str | None = None, tags: str | None = None,
         priority: str | None = None) -> None:
    if not topic:
        print(f"[notify] {title}: {message}" + (f" -> {click_url}" if click_url else ""))
        return
    # HTTP headers must be latin-1; strip anything else (e.g. emoji) from Title/Tags.
    def _ascii(s: str) -> str:
        return s.encode("ascii", "ignore").decode("ascii")
    headers = {"Title": _ascii(title)}
    if click_url:
        headers["Click"] = click_url
    if tags:
        headers["Tags"] = tags          # emoji shortcodes, comma-separated
    if priority:
        headers["Priority"] = priority  # 1..5
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                      headers=headers, timeout=20)
    except Exception as e:
        print(f"[notify] push failed ({e}); message was: {title}: {message}")
