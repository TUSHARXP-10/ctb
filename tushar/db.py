"""SQLite storage: trades, Claude's cycles and thoughts, memories, news, equity."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL, finished_at REAL, trigger TEXT, brain TEXT, model TEXT,
    status TEXT, num_turns INTEGER, duration_ms INTEGER, cost_usd REAL,
    input_tokens INTEGER, output_tokens INTEGER, summary TEXT, error TEXT, actions TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER, ts REAL, kind TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_cycle ON events(cycle_id);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, side TEXT, qty REAL, entry_price REAL,
    stop_loss REAL, take_profit REAL, initial_stop REAL, leverage INTEGER,
    risk_usd REAL, risk_pct REAL, notional REAL,
    opened_at REAL, closed_at REAL, status TEXT,
    exit_price REAL, pnl REAL, fees REAL, r_multiple REAL, exit_reason TEXT,
    setup TEXT, confidence REAL, thesis TEXT, close_reason TEXT,
    cycle_id INTEGER, tm TEXT, review TEXT, reviewed INTEGER DEFAULT 0, extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, kind TEXT, symbol TEXT, text TEXT, importance INTEGER, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, symbol TEXT, condition TEXT, price REAL, note TEXT,
    status TEXT, triggered_at REAL, cycle_id INTEGER
);
CREATE TABLE IF NOT EXISTS equity (ts REAL PRIMARY KEY, equity REAL, wallet REAL, upnl REAL);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, ts REAL, source TEXT, title TEXT, link TEXT,
    summary TEXT, tags TEXT, symbols TEXT, sentiment REAL
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts);
CREATE TABLE IF NOT EXISTS outlooks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, cycle_id INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS sims (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, side TEXT, params TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, role TEXT, text TEXT);
"""

JSON_COLUMNS = {"actions", "data", "tm", "extra", "tags", "symbols", "params", "result"}


class DB:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    # ---------------- low level ----------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [_decode(dict(r)) for r in rows]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, row: dict) -> int:
        row = {k: _encode(k, v) for k, v in row.items()}
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", row.values())
        return int(cur.lastrowid)

    def update(self, table: str, row_id: Any, changes: dict, key: str = "id") -> None:
        if not changes:
            return
        changes = {k: _encode(k, v) for k, v in changes.items()}
        sets = ",".join(f"{k}=?" for k in changes)
        self.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*changes.values(), row_id])

    # ---------------- key/value state ----------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    def get_settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.query("SELECT key, value FROM settings")}

    def save_settings(self, values: dict) -> None:
        for k, v in values.items():
            self.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (k, json.dumps(v)))

    # ---------------- trades ----------------
    def open_trades(self) -> list[dict]:
        return self.query("SELECT * FROM trades WHERE status='open' ORDER BY opened_at")

    def open_trade_for(self, symbol: str) -> dict | None:
        return self.one("SELECT * FROM trades WHERE status='open' AND symbol=? ORDER BY opened_at DESC", (symbol,))

    def recent_trades(self, limit: int = 50, status: str | None = None) -> list[dict]:
        if status:
            return self.query("SELECT * FROM trades WHERE status=? ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?",
                              (status, limit))
        return self.query("SELECT * FROM trades ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?", (limit,))

    def trades_since(self, ts: float) -> list[dict]:
        return self.query("SELECT * FROM trades WHERE opened_at>=? ORDER BY opened_at", (ts,))

    def closed_since(self, ts: float) -> list[dict]:
        return self.query("SELECT * FROM trades WHERE status='closed' AND closed_at>=? ORDER BY closed_at", (ts,))

    def unreviewed_closed(self) -> list[dict]:
        return self.query("SELECT * FROM trades WHERE status='closed' AND reviewed=0 ORDER BY closed_at")

    # ---------------- memory ----------------
    def memories(self, limit: int = 20) -> list[dict]:
        return self.query("SELECT * FROM memories WHERE active=1 ORDER BY importance DESC, ts DESC LIMIT ?", (limit,))

    # ---------------- events ----------------
    def add_event(self, cycle_id: int | None, kind: str, data: dict) -> None:
        self.insert("events", {"cycle_id": cycle_id, "ts": time.time(), "kind": kind, "data": data})

    def cycle_events(self, cycle_id: int) -> list[dict]:
        return self.query("SELECT * FROM events WHERE cycle_id=? ORDER BY id", (cycle_id,))


def _encode(key: str, value: Any) -> Any:
    if key in JSON_COLUMNS and value is not None and not isinstance(value, str):
        return json.dumps(value, default=str)
    return value


def _decode(row: dict) -> dict:
    for k in JSON_COLUMNS & row.keys():
        v = row[k]
        if isinstance(v, str) and v[:1] in "[{":
            try:
                row[k] = json.loads(v)
            except ValueError:
                pass
    return row
