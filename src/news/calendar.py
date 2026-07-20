"""Economic calendar filter (cached JSON + optional fetch)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import NewsConfig
from src.filters import check_news_blackout

logger = logging.getLogger(__name__)


# Built-in sample high-impact crypto/macro events for offline tests / backtests.
# In production, refresh via refresh_calendar() or drop a JSON file at cache_path.
SAMPLE_EVENTS: list[dict[str, Any]] = [
    {
        "title": "FOMC Rate Decision",
        "impact": "high",
        "currency": "USD",
        "time": "2024-06-12T18:00:00+00:00",
    },
    {
        "title": "CPI m/m",
        "impact": "high",
        "currency": "USD",
        "time": "2024-07-11T12:30:00+00:00",
    },
]


class EconomicCalendar:
    def __init__(self, cfg: NewsConfig) -> None:
        self.cfg = cfg
        self._events: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        path = Path(self.cfg.cache_path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    events = data.get("events") or []
                elif isinstance(data, list):
                    events = data
                else:
                    events = []
                if events:
                    self._events = list(events)
                    logger.info("Loaded %d calendar events from %s", len(self._events), path)
                    return
                logger.warning("Calendar file %s is empty — using built-in sample events", path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load calendar cache: %s", exc)
        self._events = list(SAMPLE_EVENTS)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.save()

    def save(self) -> None:
        path = Path(self.cfg.cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"events": self._events}, indent=2),
            encoding="utf-8",
        )

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._events

    def set_events(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.save()

    def is_blackout(self, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        result = check_news_blackout(now, self._events, self.cfg)
        if result.allowed:
            return False, ""
        return True, ";".join(result.reasons)

    def refresh_from_static_window(
        self, start: datetime, end: datetime, seed_events: list[dict[str, Any]] | None = None
    ) -> None:
        """Filter/replace events for a backtest window (no live scrape dependency)."""
        events = seed_events if seed_events is not None else self._events
        kept: list[dict[str, Any]] = []
        for ev in events:
            ts = ev.get("time")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if not isinstance(ts, datetime):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if start <= ts <= end:
                kept.append({**ev, "time": ts.isoformat()})
        self._events = kept

    def add_synthetic_high_impact(self, when: datetime, title: str = "Synthetic High Impact") -> None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        self._events.append(
            {"title": title, "impact": "high", "currency": "USD", "time": when.isoformat()}
        )


def generate_recurring_placeholders(
    start: datetime, end: datetime, weekday: int = 2, hour: int = 18
) -> list[dict[str, Any]]:
    """Weekly placeholder high-impact events for long backtests (e.g. Wed 18:00 UTC)."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    events: list[dict[str, Any]] = []
    # Align to first matching weekday
    d = start
    while d.weekday() != weekday:
        d += timedelta(days=1)
    d = d.replace(hour=hour, minute=0, second=0, microsecond=0)
    while d <= end:
        events.append(
            {
                "title": "Macro High Impact Placeholder",
                "impact": "high",
                "currency": "USD",
                "time": d.isoformat(),
            }
        )
        d += timedelta(days=7)
    return events
