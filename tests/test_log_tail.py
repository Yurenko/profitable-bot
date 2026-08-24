"""Tail log file without reading the whole contents."""
from __future__ import annotations

from pathlib import Path

from src.logging_setup import resolve_log_file, tail_log_lines


def test_tail_log_lines_last_n(tmp_path: Path) -> None:
    p = tmp_path / "bot.log"
    p.write_text("\n".join(f"line-{i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    assert tail_log_lines(p, 3) == ["line-18", "line-19", "line-20"]


def test_tail_missing_file(tmp_path: Path) -> None:
    assert tail_log_lines(tmp_path / "nope.log", 10) == []


def test_resolve_log_file_relative_to_root(tmp_path: Path) -> None:
    path = resolve_log_file({"file": "logs/bot.log"}, project_root=tmp_path)
    assert path == tmp_path / "logs" / "bot.log"
