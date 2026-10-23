"""FastAPI app: the dashboard's REST API, live WebSocket and static files."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .engine import Engine

log = logging.getLogger("tushar.server")
WEB = Path(__file__).parent / "web"
INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


def _clean(obj):
    """Make anything JSON-safe for the browser (no NaN/inf, no Decimals)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    return str(obj)


def _json(obj) -> str:
    return json.dumps(_clean(obj))


def _session_secret(data_dir: str) -> str:
    """Stable per-install secret so dashboard logins survive restarts."""
    path = Path(data_dir) / ".session_secret"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            value = path.read_text().strip()
            if len(value) >= 32:
                return value
        value = secrets.token_hex(32)
        path.write_text(value)
        return value
    except OSError:
        return secrets.token_hex(32)


def create_app(settings: Settings | None = None, engine: Engine | None = None, start_loops: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    secret = _session_secret(settings.data_dir)

    def session_token() -> str:
        return hmac.new(secret.encode(), settings.dashboard_password.encode(), hashlib.sha256).hexdigest()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        eng = engine or Engine(settings)
        app.state.engine = eng
        app.state.start_error = None
        try:
            await eng.start(loops=start_loops)
        except Exception as e:  # noqa: BLE001
            app.state.start_error = f"{type(e).__name__}: {e}"
            log.exception("engine failed to start")
        yield
        await eng.stop()

    app = FastAPI(title="Tushar.ai", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    def authed(request_cookies) -> bool:
        if not settings.dashboard_password:
            return True
        return hmac.compare_digest(request_cookies.get("tushar_session", ""), session_token())

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/login" and not authed(request.cookies):
            return JSONResponse({"error": "login required"}, status_code=401)
        return await call_next(request)

    def eng(request: Request) -> Engine:
        e: Engine = request.app.state.engine
        if request.app.state.start_error:
            raise HTTPException(503, f"engine failed to start: {request.app.state.start_error}")
        return e

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if not authed(request.cookies):
            return RedirectResponse("/login")
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return FileResponse(WEB / "login.html")

    failures: list[float] = []

    @app.post("/api/login")
    async def login(request: Request):
        now = time.time()
        failures[:] = [t for t in failures if now - t < 600]
        if len(failures) >= 10:
            return JSONResponse({"ok": False, "error": "too many attempts, wait 10 minutes"}, status_code=429)
        body = await request.json()
        if not settings.dashboard_password or hmac.compare_digest(str(body.get("password", "")).strip(),
                                                                  settings.dashboard_password):
            resp = JSONResponse({"ok": True})
            resp.set_cookie("tushar_session", session_token(), httponly=True, samesite="lax", max_age=30 * 86400)
            return resp
        failures.append(now)
        await asyncio.sleep(1.0)
        return JSONResponse({"ok": False, "error": "wrong password"}, status_code=401)

    @app.get("/healthz")
    async def healthz(request: Request):
        e: Engine = request.app.state.engine
        ok = e.ready and not request.app.state.start_error
        return JSONResponse({"ok": ok, "error": request.app.state.start_error}, status_code=200 if ok else 503)

    # ---------------------------------------------------------------- state
    @app.get("/api/state")
    async def state(request: Request):
        if request.app.state.start_error:
            return JSONResponse({"start_error": request.app.state.start_error,
                                 "settings": settings.public_dict()}, status_code=503)
        return JSONResponse(_clean(await eng(request).full_state()))

    @app.get("/api/klines")
    async def klines(request: Request, symbol: str, interval: str = "15m", limit: int = 400):
        e = eng(request)
        symbol = symbol.upper()
        if interval not in INTERVALS:
            raise HTTPException(400, "bad interval")
        if symbol not in e.market.symbols:
            raise HTTPException(404, "unknown symbol")
        limit = max(50, min(limit, 1000))
        candles = await e.market.klines(symbol, interval, limit)
        trades = e.db.query("SELECT id, side, entry_price, exit_price, opened_at, closed_at, status, pnl, "
                            "stop_loss, take_profit, exit_reason, setup FROM trades WHERE symbol=? "
                            "ORDER BY opened_at DESC LIMIT 60", (symbol,))
        return {"symbol": symbol, "interval": interval,
                "candles": [{"time": c["t"] // 1000, "open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"],
                             "volume": c["qv"]} for c in candles],
                "trades": trades, "price": e.market.price(symbol)}

    @app.get("/api/network")
    async def network(request: Request):
        from .netutil import PHONE, dashboard_urls, login_link
        urls = dashboard_urls(settings.host, settings.port)
        key = settings.dashboard_password
        public = PHONE.get("public_url")
        return {"host": settings.host, "port": settings.port, "urls": urls, "password_set": bool(key),
                "phone_ready": settings.host not in ("127.0.0.1", "localhost"),
                "public_url": public, "tunnel": PHONE.get("tunnel"),
                "public_login": login_link(public, key) if public and key else public,
                "lan_login": login_link(urls[0], key) if urls and key else (urls[0] if urls else None)}

    @app.get("/api/symbols")
    async def symbols(request: Request):
        e = eng(request)
        return {"universe": e.market.universe(60), "watchlist": e.s.watchlist}

    @app.get("/api/cycles/{cid}")
    async def cycle(request: Request, cid: int):
        e = eng(request)
        row = e.db.one("SELECT * FROM cycles WHERE id=?", (cid,))
        if not row:
            raise HTTPException(404)
        return {"cycle": row, "events": e.db.cycle_events(cid),
                "reports": e.db.query("SELECT agent, data, ts FROM reports WHERE cycle_id=? ORDER BY id", (cid,))}

    @app.get("/api/correlations")
    async def correlations(request: Request, hours: int = 168):
        return JSONResponse(_clean(await eng(request).desk.get_correlations(None, hours)))

    @app.get("/api/liquidations")
    async def liquidations(request: Request, minutes: float = 60):
        e = eng(request)
        return JSONResponse(_clean({"summary": e.radar.summary(minutes, 10), "recent": e.radar.recent(40),
                                    "buckets": e.radar.buckets(120, 5)}))

    @app.get("/api/sims/{sid}")
    async def sim(request: Request, sid: int):
        row = eng(request).db.one("SELECT * FROM sims WHERE id=?", (sid,))
        if not row:
            raise HTTPException(404)
        return row

    @app.get("/api/trades")
    async def trades(request: Request, limit: int = 100):
        return eng(request).db.recent_trades(min(limit, 500))

    @app.get("/api/equity")
    async def equity(request: Request, hours: float = 168):
        rows = eng(request).db.query("SELECT ts, equity, wallet, upnl FROM equity WHERE ts>=? ORDER BY ts",
                                     (time.time() - hours * 3600,))
        return rows

    # ---------------------------------------------------------------- controls
    @app.post("/api/control/{action}")
    async def control(request: Request, action: str):
        e = eng(request)
        if action == "run":
            return await e.run_now()
        if action == "pause":
            e.pause()
        elif action == "resume":
            e.resume()
        elif action == "kill":
            closed = await e.kill()
            return {"ok": True, "closed": closed}
        elif action == "reset_kill":
            e.reset_kill()
        elif action == "scan":
            res = await e.scanner.scan(max_age=0)
            e.bus.publish("scanner", res)
        elif action == "news":
            n = await e.news.refresh()
            return {"ok": True, "new": n}
        elif action == "report":
            rep = await e.make_daily_report()
            return {"ok": True, "report": rep}
        else:
            raise HTTPException(404, "unknown action")
        return {"ok": True, "status": e.status()}

    @app.post("/api/positions/{symbol}/close")
    async def close_position(request: Request, symbol: str):
        return await eng(request).close_manual(symbol)

    @app.post("/api/settings")
    async def update_settings(request: Request):
        body = await request.json()
        changed = eng(request).update_settings(body or {})
        return {"ok": True, "changed": changed, "settings": settings.public_dict()}

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        msg = str(body.get("message", "")).strip()
        if not msg:
            raise HTTPException(400, "empty message")
        e = eng(request)
        asyncio.create_task(e.chat(msg[:4000]))
        return {"ok": True}

    @app.post("/api/alerts/{aid}/cancel")
    async def cancel_alert(request: Request, aid: int):
        return await eng(request).desk.cancel_alert(aid)

    @app.post("/api/pending/{pid}/cancel")
    async def cancel_pending(request: Request, pid: int):
        return await eng(request).desk.cancel_pending_entry(pid)

    @app.post("/api/memories/{mid}/forget")
    async def forget(request: Request, mid: int):
        return await eng(request).desk.forget_memory(mid)

    @app.post("/api/timemachine")
    async def timemachine(request: Request):
        body = await request.json()
        e = eng(request)
        res = await e.desk.run_time_machine(body["symbol"], body["side"], float(body["stop_loss"]),
                                            float(body["take_profit"]), body.get("entry"),
                                            float(body.get("horizon_hours", 24)))
        return JSONResponse(_clean(res))

    # ---------------------------------------------------------------- live
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if not authed(websocket.cookies):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        e: Engine = websocket.app.state.engine
        q = e.bus.subscribe()

        async def pump():
            while True:
                msg = await q.get()
                await websocket.send_text(_json(msg))

        async def listen():
            while True:
                await websocket.receive_text()

        try:
            tasks = [asyncio.create_task(pump()), asyncio.create_task(listen())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
        except WebSocketDisconnect:
            pass
        except Exception as ex:  # noqa: BLE001
            log.debug("ws closed: %s", ex)
        finally:
            e.bus.unsubscribe(q)

    return app

