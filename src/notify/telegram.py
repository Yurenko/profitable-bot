"""Telegram trade notifications (free Bot API). Fail-soft — never breaks trading."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        *,
        timeout_sec: float = 8.0,
    ) -> None:
        self.token = (token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (
            chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        ).strip()
        self.timeout_sec = timeout_sec

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                logger.warning("Telegram API not ok: %s", body)
                return False
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
            logger.warning("Telegram send failed: %s", exc)
            return False

    def notify_enter(
        self,
        *,
        symbol: str,
        price: float,
        qty: float,
        avg_entry: float,
        dca_level: int,
        mode: str,
        margin: float | None = None,
    ) -> None:
        lines = [
            "🟢 Відкрито позицію",
            f"Монета: {symbol}",
            f"Ціна: {price:.4f}",
            f"К-сть: {qty:.6f}",
            f"Avg entry: {avg_entry:.4f}",
            f"DCA level: {dca_level}",
            f"Режим: {mode}",
        ]
        if margin is not None:
            lines.insert(4, f"Маржа: ${margin:.2f}")
        self.send("\n".join(lines))

    def notify_dca(
        self,
        *,
        symbol: str,
        price: float,
        qty: float,
        avg_entry: float,
        dca_level: int,
        mode: str,
        margin: float | None = None,
    ) -> None:
        lines = [
            "🔵 Усереднення (DCA)",
            f"Монета: {symbol}",
            f"Ціна докупівлі: {price:.4f}",
            f"К-сть: {qty:.6f}",
            f"Новий avg entry: {avg_entry:.4f}",
            f"DCA level: {dca_level}",
            f"Режим: {mode}",
        ]
        if margin is not None:
            lines.insert(4, f"Маржа: ${margin:.2f}")
        self.send("\n".join(lines))

    def notify_close(
        self,
        *,
        symbol: str,
        action: str,
        price: float,
        qty: float,
        avg_entry: float,
        pnl: float | None,
        mode: str,
    ) -> None:
        action_ua = {
            "full_tp": "повне закриття (TP)",
            "partial_tp": "часткове закриття (TP)",
            "trail_exit": "вихід (trailing)",
        }.get(action, action)
        pnl_s = "—"
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            pnl_s = f"{sign}${pnl:.2f}"
        emoji = "🔴" if (pnl is not None and pnl < 0) else "✅"
        self.send(
            "\n".join(
                [
                    f"{emoji} Закрито позицію — {action_ua}",
                    f"Монета: {symbol}",
                    f"Ціна: {price:.4f}",
                    f"К-сть: {qty:.6f}",
                    f"Avg entry: {avg_entry:.4f}",
                    f"PnL: {pnl_s}",
                    f"Режим: {mode}",
                ]
            )
        )

    def notify_margin_topup(self, *, symbol: str, amount: float, mode: str) -> None:
        self.send(
            "\n".join(
                [
                    "🟡 Додано маржу",
                    f"Монета: {symbol}",
                    f"Сума: ${amount:.2f}",
                    f"Режим: {mode}",
                ]
            )
        )

    def notify_order_error(self, *, symbol: str, error: str, mode: str) -> None:
        # Keep short — Telegram has message length limits; errors can be long
        err = (error or "")[:400]
        self.send(
            "\n".join(
                [
                    "⚠️ Помилка ордера",
                    f"Монета: {symbol}",
                    f"Режим: {mode}",
                    f"Помилка: {err}",
                ]
            )
        )


def format_test_message() -> str:
    return "MRDCA Bot: Telegram підключено ✅"


def notify_from_env() -> TelegramNotifier:
    return TelegramNotifier()
