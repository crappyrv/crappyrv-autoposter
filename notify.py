"""
notify.py — observability: the notify() hook + shared logging setup.

For now notify() routes through the logger (which fans out to the console and the
rotating log file). Email/Slack get added here later so the rest of the codebase
never changes how it raises an alert.

setup_logging() lives here too so every entry point (main.py, publish.py,
dropbox_client.py, metadata.py) configures logging identically — console gets a
clean one-line message, the rotating file gets full tracebacks.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config import Settings

logger = logging.getLogger("notify")


class _ConsoleFormatter(logging.Formatter):
    """Console formatter that NEVER prints tracebacks (they go to the log file).

    Keeps runs readable: console shows a one-line error; the rotating log file
    keeps the full traceback for forensics.
    """

    def format(self, record: logging.LogRecord) -> str:
        saved = (record.exc_info, record.exc_text, record.stack_info)
        record.exc_info = record.exc_text = record.stack_info = None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text, record.stack_info = saved


def setup_logging(cfg: Settings) -> None:
    """Configure root logging once: rotating file (full detail) + console (clean)."""
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(getattr(logging, cfg.logging.level.upper(), logging.INFO))

    log_path = cfg.project_root / cfg.logging.file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

    fh = RotatingFileHandler(
        log_path, maxBytes=cfg.logging.max_bytes, backupCount=cfg.logging.backup_count
    )
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(_ConsoleFormatter(fmt))
    root.addHandler(sh)


def notify(message: str, *, level: str = "INFO") -> None:
    """
    Send a human-facing notification.

    Currently routes through the logger (console + rotating file). The log
    handlers are the always-on fallback.

    TODO: add email and/or Slack delivery here (read channel config from .env /
    config.yaml). Keep the log as the always-on fallback.
    """
    lvl = getattr(logging, level.upper(), logging.INFO)
    logger.log(lvl, "NOTIFY: %s", message)
