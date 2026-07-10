"""
Merch auto-poster — typed settings.

Secrets come from .env (shared with the video auto-poster in the parent dir).
Non-secret product config comes from merch/blanks.yaml.

Run `python merch/config.py` to sanity-check that everything loads. It prints
non-secret settings and, for each secret, only whether it is present — never the
value itself.
"""
from __future__ import annotations
import os
from pathlib import Path
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
REPO = HERE.parent  # video-autoposter/ — .env lives here

# Load the shared .env from the repo root (falls back to CWD).
for candidate in (REPO / ".env", HERE / ".env"):
    if candidate.exists():
        load_dotenv(candidate)
        break


@dataclass(frozen=True)
class Secrets:
    anthropic_api_key: str
    dropbox_app_key: str
    dropbox_app_secret: str
    dropbox_refresh_token: str
    printify_api_token: str
    printify_shop_id: str  # the Etsy-connected Printify shop id (from discover_blanks)
    ntfy_topic: str  # optional; "" disables phone pings


@dataclass(frozen=True)
class Settings:
    secrets: Secrets
    blanks: dict = field(default_factory=dict)

    # ----- convenience accessors on blanks.yaml -----
    @property
    def drop_root(self) -> str:
        return self.blanks["drop_root"]

    @property
    def printfiles_folder(self) -> str:
        return self.blanks["printfiles_folder"]

    @property
    def done_folder(self) -> str:
        return self.blanks["done_folder"]

    @property
    def failed_folder(self) -> str:
        return self.blanks["failed_folder"]

    @property
    def image_extensions(self) -> list[str]:
        return [e.lower() for e in self.blanks["image_extensions"]]

    @property
    def product_types(self) -> dict:
        return self.blanks["product_types"]


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    return val


def load_settings() -> Settings:
    secrets = Secrets(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        dropbox_app_key=_require("DROPBOX_APP_KEY"),
        dropbox_app_secret=_require("DROPBOX_APP_SECRET"),
        dropbox_refresh_token=_require("DROPBOX_REFRESH_TOKEN"),
        printify_api_token=_require("PRINTIFY_API_TOKEN"),
        printify_shop_id=_require("PRINTIFY_SHOP_ID"),
        ntfy_topic=_require("NTFY_TOPIC"),
    )
    with open(HERE / "blanks.yaml", "r", encoding="utf-8") as f:
        blanks = yaml.safe_load(f)
    return Settings(secrets=secrets, blanks=blanks)


def _mask(val: str) -> str:
    return "SET" if val else "-- MISSING --"


if __name__ == "__main__":
    s = load_settings()
    print("== merch config ==")
    print("drop_root         :", s.drop_root)
    print("product types     :", ", ".join(s.product_types.keys()))
    print()
    print("== secrets (presence only) ==")
    print("ANTHROPIC_API_KEY   :", _mask(s.secrets.anthropic_api_key))
    print("DROPBOX_APP_KEY     :", _mask(s.secrets.dropbox_app_key))
    print("DROPBOX_APP_SECRET  :", _mask(s.secrets.dropbox_app_secret))
    print("DROPBOX_REFRESH_TOKEN:", _mask(s.secrets.dropbox_refresh_token))
    print("PRINTIFY_API_TOKEN  :", _mask(s.secrets.printify_api_token))
    print("PRINTIFY_SHOP_ID    :", _mask(s.secrets.printify_shop_id))
    print("NTFY_TOPIC (optional):", _mask(s.secrets.ntfy_topic))
