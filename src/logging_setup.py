"""Logging setup (Europe/Kyiv timestamps)."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_KYIV = ZoneInfo("Europe/Kyiv")


class KyivFormatter(logging.Formatter):
    """Format asctime in Europe/Kyiv regardless of server TZ (AWS is usually UTC)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=_KYIV)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def setup_logging(cfg: dict[str, Any] | None = None) -> None:
    cfg = cfg or {}
    level = getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    fmt = KyivFormatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    if cfg.get("console", True):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    log_file = cfg.get("file")
    if log_file:
        path = Path(log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # On Vercel the filesystem is often read-only except /tmp.
            path = Path("/tmp") / path.name
            path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
