"""FastAPI dashboard server."""
from __future__ import annotations

import asyncio
import json
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from dashboard.auth import (
    COOKIE_NAME,
    SESSION_TTL_SEC,
    auth_required,
    check_password,
    is_authenticated,
    make_session_token,
)
from dashboard.bot_manager import BotManager
from src.config import load_config
from src.config_io import config_to_api_dict

STATIC = Path(__file__).resolve().parent / "static"
manager = BotManager()

# Paths that do not require a session cookie
_PUBLIC_EXACT = {"/login", "/api/login", "/api/auth/status", "/favicon.ico"}


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


class LoginRequest(BaseModel):
    password: str = ""


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_required():
            return await call_next(request)

        path = request.url.path
        if path in _PUBLIC_EXACT:
            return await call_next(request)

        token = request.cookies.get(COOKIE_NAME)
        if is_authenticated(token):
            return await call_next(request)

        # HTML / root → login page; API / static → 401
        accept = request.headers.get("accept", "")
        if path == "/" or "text/html" in accept:
            return RedirectResponse(url="/login", status_code=302)
        return JSONResponse(
            {"ok": False, "error": "Unauthorized", "login": "/login"},
            status_code=401,
        )


app = FastAPI(title="MRDCA Trading Dashboard", version="1.0.0")
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.on_event("startup")
async def _resume_bot_on_startup() -> None:
    """Resume bot thread if bot_active was persisted (dashboard-only 24/7 setup)."""
    manager.ensure_started(default_mode="paper")


@app.get("/login")
async def login_page(request: Request):
    if is_authenticated(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(url="/", status_code=302)
    return FileResponse(STATIC / "login.html")


@app.get("/api/auth/status")
async def api_auth_status(request: Request) -> dict:
    return {
        "auth_required": auth_required(),
        "authenticated": is_authenticated(request.cookies.get(COOKIE_NAME)),
    }


@app.post("/api/login")
async def api_login(body: LoginRequest) -> JSONResponse:
    if not auth_required():
        return JSONResponse({"ok": True, "auth_required": False})
    if not check_password(body.password):
        return JSONResponse(
            {"ok": False, "error": "Невірний пароль"},
            status_code=401,
        )
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key=COOKIE_NAME,
        value=make_session_token(),
        max_age=SESSION_TTL_SEC,
        httponly=True,
        samesite="lax",
        path="/",
        # HTTP on VPS IP:8080 — Secure cookie would break login without HTTPS
        secure=False,
    )
    return resp


@app.post("/api/logout")
async def api_logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


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


@app.get("/api/trades")
async def api_trades(limit: int = 100) -> list:
    return manager.list_trades(limit=min(limit, 500))


@app.get("/api/logs")
async def api_logs(limit: int = 60) -> list:
    return manager.read_logs(limit=min(limit, 200))


@app.get("/api/history/files")
async def api_history_files() -> list:
    return manager.list_history_files()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # WebSocket bypasses HTTP middleware — check cookie here
    if not is_authenticated(ws.cookies.get(COOKIE_NAME)):
        await ws.close(code=4401)
        return
    await ws.accept()
    while True:
        try:
            payload = manager.get_status()
            payload["audit_preview"] = manager.read_audit(8)
            await ws.send_text(json.dumps(payload, default=str))
        except WebSocketDisconnect:
            break
        except Exception as exc:  # noqa: BLE001
            # Log but keep the socket alive — don't let one bad tick kill the stream
            try:
                import logging as _log

                _log.getLogger(__name__).debug("ws tick error: %s", exc)
                await ws.send_text(json.dumps({"error": str(exc)}, default=str))
            except Exception:
                break
        await asyncio.sleep(2)


def run_dashboard(host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True) -> None:
    import uvicorn

    from src.config import load_config
    from src.logging_setup import setup_logging

    try:
        setup_logging(load_config("config.yaml").logging)
    except Exception:
        pass

    url = f"http://{host}:{port}"
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=None)
