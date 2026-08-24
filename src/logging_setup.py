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


def resolve_log_file(cfg: dict[str, Any] | None = None, *, project_root: Path | None = None) -> Path:
    """Absolute path to the bot log file (relative paths are from project root)."""
    cfg = cfg or {}
    raw = str(cfg.get("file") or "logs/bot.log")
    path = Path(raw)
    if not path.is_absolute():
        root = project_root or Path(__file__).resolve().parent.parent
        path = root / path
    return path


def tail_log_lines(path: Path, limit: int = 60) -> list[str]:
    """Last `limit` lines without loading the whole file (bot.log grows for weeks)."""
    limit = max(1, int(limit))
    if not path.exists() or not path.is_file():
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= 0:
        return []
    # Read from the end in chunks until we have enough newlines
    block = 8192
    data = b""
    newlines = 0
    with path.open("rb") as f:
        pos = size
        while pos > 0 and newlines <= limit:
            read = min(block, pos)
            pos -= read
            f.seek(pos)
            chunk = f.read(read)
            data = chunk + data
            newlines += chunk.count(b"\n")
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-limit:]
