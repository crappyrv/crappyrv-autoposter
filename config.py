"""
config.py — single source of truth for configuration.

Loads secrets from `.env` and non-secret settings from `config.yaml`, validates
both with pydantic, and exposes one typed `Settings` object via `load_config()`.

Rules honored here:
  * Nothing is hardcoded — every secret comes from .env.
  * Secret VALUES are never logged. `__repr__` is overridden to redact them.
  * Missing/blank secrets fail loud at load time with a clear message.

Usage:
    from config import load_config
    cfg = load_config()
    cfg.dropbox.watch_folder
    cfg.secrets.anthropic_api_key   # value present, but never printed
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator

# --- Paths -------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
CONFIG_YAML_PATH = PROJECT_ROOT / "config.yaml"


# --- Secret settings (from .env) ---------------------------------------------
class Secrets(BaseModel):
    """Every value here is sensitive. SecretStr keeps it out of logs/reprs."""

    anthropic_api_key: SecretStr

    dropbox_app_key: SecretStr
    dropbox_app_secret: SecretStr
    dropbox_refresh_token: SecretStr

    youtube_client_id: SecretStr
    youtube_client_secret: SecretStr
    youtube_refresh_token: SecretStr

    facebook_page_id: str  # not secret per se, but lives in .env for convenience
    facebook_page_access_token: SecretStr


# --- Non-secret settings (from config.yaml) ----------------------------------
class DropboxSettings(BaseModel):
    watch_folder: str
    posted_folder: str
    failed_folder: str
    video_extensions: List[str]
    image_extensions: List[str] = Field(default_factory=list)

    @field_validator("video_extensions", "image_extensions")
    @classmethod
    def _lowercase_extensions(cls, v: List[str]) -> List[str]:
        return [ext.lower() for ext in v]


class YouTubeSettings(BaseModel):
    privacy_status: str = "unlisted"
    category_id: str = "22"
    made_for_kids: bool = False
    add_shorts_hashtag: bool = True

    @field_validator("privacy_status")
    @classmethod
    def _valid_privacy(cls, v: str) -> str:
        allowed = {"private", "unlisted", "public"}
        if v not in allowed:
            raise ValueError(f"privacy_status must be one of {allowed}, got {v!r}")
        return v


class FacebookSettings(BaseModel):
    graph_api_version: str = "v25.0"
    post_video: bool = True
    post_reel: bool = True
    post_photo: bool = True


class AnthropicSettings(BaseModel):
    model: str
    max_tokens: int = 1500


class BrandSettings(BaseModel):
    name: str
    description: str
    voice: str
    rules: List[str] = Field(default_factory=list)


class MetadataSettings(BaseModel):
    title_max_length: int = 100
    description_max_length: int = 5000
    tags_max_count: int = 15
    required_hashtags: List[str] = Field(default_factory=lambda: ["#goodluckoutthere"])


class LoggingSettings(BaseModel):
    file: str = "logs/autoposter.log"
    level: str = "INFO"
    max_bytes: int = 5_242_880
    backup_count: int = 5


class RunSettings(BaseModel):
    max_videos_per_run: int = 5
    tmp_dir: str = "tmp"
    # When True, main.py publishes immediately (no approval gate).
    auto_publish: bool = False


class Settings(BaseModel):
    """The merged, typed configuration object the rest of the app consumes."""

    secrets: Secrets
    dropbox: DropboxSettings
    youtube: YouTubeSettings
    facebook: FacebookSettings
    anthropic: AnthropicSettings
    brand: BrandSettings
    metadata: MetadataSettings
    logging: LoggingSettings
    run: RunSettings

    # Absolute path to the project root, for resolving relative paths.
    project_root: Path = Field(default=PROJECT_ROOT)


# --- Loaders -----------------------------------------------------------------
def _require_env(key: str) -> str:
    """Fetch an env var or fail loud with a clear, secret-safe message."""
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Copy .env.example to .env and fill it in (see CREDENTIALS.md)."
        )
    return val


def _load_secrets() -> Secrets:
    return Secrets(
        anthropic_api_key=_require_env("ANTHROPIC_API_KEY"),
        dropbox_app_key=_require_env("DROPBOX_APP_KEY"),
        dropbox_app_secret=_require_env("DROPBOX_APP_SECRET"),
        dropbox_refresh_token=_require_env("DROPBOX_REFRESH_TOKEN"),
        youtube_client_id=_require_env("YOUTUBE_CLIENT_ID"),
        youtube_client_secret=_require_env("YOUTUBE_CLIENT_SECRET"),
        youtube_refresh_token=_require_env("YOUTUBE_REFRESH_TOKEN"),
        facebook_page_id=_require_env("FACEBOOK_PAGE_ID"),
        facebook_page_access_token=_require_env("FACEBOOK_PAGE_ACCESS_TOKEN"),
    )


def _load_yaml() -> dict:
    if not CONFIG_YAML_PATH.exists():
        raise RuntimeError(f"Missing config file: {CONFIG_YAML_PATH}")
    with CONFIG_YAML_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


@lru_cache(maxsize=1)
def load_config() -> Settings:
    """
    Load + validate config once and cache it.

    The .env file is authoritative (override=True): a blank or stale value
    already exported in the shell would otherwise silently shadow the file —
    e.g. some environments export ANTHROPIC_API_KEY='' which would beat .env.
    For a self-contained cron app, the file is the single source of truth.
    """
    load_dotenv(ENV_PATH, override=True)
    yaml_data = _load_yaml()

    return Settings(
        secrets=_load_secrets(),
        dropbox=DropboxSettings(**yaml_data.get("dropbox", {})),
        youtube=YouTubeSettings(**yaml_data.get("youtube", {})),
        facebook=FacebookSettings(**yaml_data.get("facebook", {})),
        anthropic=AnthropicSettings(**yaml_data.get("anthropic", {})),
        brand=BrandSettings(**yaml_data.get("brand", {})),
        metadata=MetadataSettings(**yaml_data.get("metadata", {})),
        logging=LoggingSettings(**yaml_data.get("logging", {})),
        run=RunSettings(**yaml_data.get("run", {})),
    )


def update_env_var(key: str, value: str, env_path: Path = ENV_PATH) -> None:
    """Replace (or append) `key=value` in the .env file, atomically.

    Used by the one-time auth helpers to save minted refresh tokens. Never logs
    the value.
    """
    import re

    line = f"{key}={value}"
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        if re.search(rf"^{re.escape(key)}=.*$", text, flags=re.MULTILINE):
            text = re.sub(rf"^{re.escape(key)}=.*$", line, text, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + "\n" + line + "\n"
    else:
        text = line + "\n"
    tmp = env_path.with_suffix(".env.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(env_path)


if __name__ == "__main__":
    # Smoke test: prints the non-secret config and confirms secrets are present
    # WITHOUT ever printing their values.
    cfg = load_config()
    print("Config loaded OK.")
    print(f"  watch_folder      : {cfg.dropbox.watch_folder}")
    print(f"  posted_folder     : {cfg.dropbox.posted_folder}")
    print(f"  failed_folder     : {cfg.dropbox.failed_folder}")
    print(f"  yt privacy_status : {cfg.youtube.privacy_status}")
    print(f"  graph_api_version : {cfg.facebook.graph_api_version}")
    print(f"  anthropic model   : {cfg.anthropic.model}")
    print("  secrets present   : "
          + ", ".join(
              k for k in (
                  "anthropic_api_key", "dropbox_refresh_token",
                  "youtube_refresh_token", "facebook_page_access_token",
              )
          ))
