"""FastAPI dashboard server."""
from __future__ import annotations

import asyncio
import json
import webbrowser
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dashboard.bot_manager import BotManager
from src.config import load_config
from src.config_io import config_to_api_dict

STATIC = Path(__file__).resolve().parent / "static"
manager = BotManager()


class ConfigPatch(BaseModel):
    strategy: dict | None = None
    risk: dict | None = None
    symbols: list[str] | None = None
    exchange: dict | None = None
    news: dict | None = None
    loop: dict | None = None
    backtest: dict | None = None


class StartRequest(BaseModel):
    mode: str = "paper"
    mainnet_ok: bool = False


class BacktestRequest(BaseModel):
    bars: int = Field(default=0, ge=0, le=5_000_000)  # 0 = усі бари з файлу
    monte_carlo: bool = True


class DownloadRequest(BaseModel):
    symbol: str = "SOL/USDT:USDT"
    since: str = "2021-01-01T00:00:00+00:00"
    timeframe: str = "1m"


app = FastAPI(title="MRDCA Trading Dashboard", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def api_status() -> dict:
    return manager.get_status()


@app.get("/api/config")
async def api_config() -> dict:
    cfg = load_config(manager.config_path)
    return config_to_api_dict(cfg)


@app.patch("/api/config")
async def api_config_patch(body: ConfigPatch) -> dict:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return {"ok": False, "error": "Empty patch"}
    try:
        cfg = manager.apply_config(patch)
        return {"ok": True, "config": config_to_api_dict(cfg)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.post("/api/bot/start")
async def api_bot_start(req: StartRequest) -> dict:
    return manager.start(req.mode, mainnet_ok=req.mainnet_ok)


@app.post("/api/bot/stop")
async def api_bot_stop() -> dict:
    return manager.stop()


@app.post("/api/bot/tick")
async def api_bot_tick() -> dict:
    try:
        return manager.tick_once("paper")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.post("/api/backtest")
async def api_backtest(req: BacktestRequest) -> dict:
    bt = manager.get_backtest()
    if bt.running:
        return {"ok": False, "error": "Бектест вже виконується"}
    manager.run_backtest(bars=req.bars, monte_carlo=req.monte_carlo)
    return {"ok": True}


@app.post("/api/history/download")
async def api_history_download(req: DownloadRequest) -> dict:
    try:
        manager.start_history_download(
            symbol=req.symbol.strip(),
            since=req.since.strip(),
            timeframe=req.timeframe.strip(),
        )
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.post("/api/equity/reset")
async def api_equity_reset() -> dict:
    try:
        return manager.reset_equity_to_deposit()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.get("/api/audit")
async def api_audit(limit: int = 80) -> list:
    return manager.read_audit(limit=min(limit, 200))


@app.get("/api/logs")
async def api_logs(limit: int = 60) -> list:
    return manager.read_logs(limit=min(limit, 200))


@app.get("/api/history/files")
async def api_history_files() -> list:
    return manager.list_history_files()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            payload = manager.get_status()
            payload["audit_preview"] = manager.read_audit(8)
            await ws.send_text(json.dumps(payload, default=str))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass


def run_dashboard(host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True) -> None:
    import uvicorn

    url = f"http://{host}:{port}"
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=host, port=port, log_level="info")
