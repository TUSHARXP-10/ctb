"""Settings for Tushar.ai.

Two layers:
  * Static settings come from the environment / .env file (keys, mode, server).
    Changing them needs a restart.
  * Runtime settings (risk limits, watchlist, Claude model, schedule) start from
    the environment and can be changed live from the dashboard. Overrides are
    saved in the database and survive restarts.

Every runtime value is clamped to a hard range, so neither a typo in .env nor
a request from the dashboard can push a limit somewhere unsafe.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

try:  # optional: plain environment variables work without it
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent

# Binance USD-M futures REST hosts.
BINANCE_URLS = {
    "demo": "https://demo-fapi.binance.com",        # demo.binance.com keys
    "testnet": "https://testnet.binancefuture.com",  # legacy futures testnet keys
    "live": "https://fapi.binance.com",
}
# Public market data host used by paper mode.
PUBLIC_MARKET_URL = "https://fapi.binance.com"

DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]


def _split(value: str) -> list[str]:
    return [s.strip().upper() for s in value.replace(";", ",").split(",") if s.strip()]


# name -> (type, min, max). Only these can be changed from the dashboard.
RUNTIME_RULES: dict[str, tuple[type, Any, Any]] = {
    "claude_model": (str, None, None),
    "claude_allow_web": (bool, None, None),
    "claude_max_turns": (int, 5, 120),
    "claude_effort": (str, None, None),
    "cycle_minutes": (int, 3, 240),
    "min_cycle_gap_minutes": (int, 1, 60),
    "watchlist": (list, None, None),
    "scan_top_n": (int, 5, 150),
    "max_leverage": (int, 1, 50),
    "default_leverage": (int, 1, 50),
    "risk_per_trade_pct": (float, 0.1, 5.0),
    "max_position_margin_pct": (float, 1.0, 100.0),
    "max_open_positions": (int, 1, 15),
    "max_daily_loss_pct": (float, 0.5, 30.0),
    "max_trades_per_day": (int, 1, 100),
    "min_reward_risk": (float, 0.5, 10.0),
    "min_stop_pct": (float, 0.05, 5.0),
    "max_stop_pct": (float, 0.5, 30.0),
    "cooldown_minutes_after_loss": (int, 0, 1440),
    "allow_widen_stop": (bool, None, None),
    "tm_gate": (bool, None, None),
    "tm_min_avg_r": (float, -1.0, 1.0),
    "tm_replays": (int, 500, 50000),
    "blocked_symbols": (list, None, None),
    "big_move_pct": (float, 0.3, 20.0),
    "chat_can_trade": (bool, None, None),
}


@dataclass
class Settings:
    # ---- static (environment, restart to change) ----
    trading_mode: str = "demo"          # demo | testnet | live | paper
    market_source: str = "binance"      # binance | sim (synthetic prices for offline testing)
    binance_api_key: str = ""
    binance_api_secret: str = ""
    allow_live: bool = False            # live trading needs this AND trading_mode=live
    brain: str = "agent"                # agent (Claude Agent SDK) | cli (claude -p) | mock
    claude_cli_path: str = ""           # optional custom path to the `claude` binary
    claude_use_subscription: bool = True  # strip ANTHROPIC_API_KEY so the Claude login is used
    margin_type: str = "ISOLATED"
    paper_start_balance: float = 10_000.0
    taker_fee_pct: float = 0.05
    host: str = "127.0.0.1"
    port: int = 8000
    dashboard_password: str = ""
    data_dir: str = str(ROOT / "data")
    sim_seed: int = 7

    # ---- runtime (dashboard-editable) ----
    claude_model: str = "sonnet"
    claude_allow_web: bool = True
    claude_max_turns: int = 40
    claude_effort: str = ""
    cycle_minutes: int = 15
    min_cycle_gap_minutes: int = 3
    watchlist: list[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    scan_top_n: int = 40
    max_leverage: int = 10
    default_leverage: int = 5
    risk_per_trade_pct: float = 1.5
    max_position_margin_pct: float = 25.0
    max_open_positions: int = 4
    max_daily_loss_pct: float = 6.0
    max_trades_per_day: int = 12
    min_reward_risk: float = 1.5
    min_stop_pct: float = 0.25
    max_stop_pct: float = 8.0
    cooldown_minutes_after_loss: int = 30
    allow_widen_stop: bool = False
    tm_gate: bool = True
    tm_min_avg_r: float = -0.15
    tm_replays: int = 10_000
    blocked_symbols: list[str] = field(default_factory=list)
    big_move_pct: float = 2.0
    chat_can_trade: bool = False

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = None) -> "Settings":
        if load_dotenv is not None:
            load_dotenv(env_file or ROOT / ".env", override=False)
        s = cls()
        for f in fields(cls):
            raw = os.environ.get(f.name.upper())
            if raw is None or raw == "":
                continue
            try:
                setattr(s, f.name, _coerce(raw, type(getattr(s, f.name))))
            except (TypeError, ValueError):
                raise ValueError(f"Bad value for {f.name.upper()}: {raw!r}") from None
        s.trading_mode = s.trading_mode.lower()
        s.market_source = s.market_source.lower()
        s.brain = s.brain.lower()
        s.margin_type = s.margin_type.upper()
        s._clamp_all()
        return s

    def apply_overrides(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """Apply runtime overrides (from the DB or the dashboard). Returns what changed."""
        changed: dict[str, Any] = {}
        for key, value in overrides.items():
            if key not in RUNTIME_RULES:
                continue
            typ, lo, hi = RUNTIME_RULES[key]
            try:
                val = _coerce(value, typ)
            except (TypeError, ValueError):
                continue
            val = _clamp(val, lo, hi)
            if getattr(self, key) != val:
                setattr(self, key, val)
                changed[key] = val
        self._clamp_all()
        return changed

    def _clamp_all(self) -> None:
        for key, (typ, lo, hi) in RUNTIME_RULES.items():
            setattr(self, key, _clamp(getattr(self, key), lo, hi))
        self.default_leverage = min(self.default_leverage, self.max_leverage)
        if self.min_stop_pct >= self.max_stop_pct:
            self.min_stop_pct = min(0.25, self.max_stop_pct / 2)
        self.watchlist = [s for s in dict.fromkeys(self.watchlist) if s][:30] or list(DEFAULT_WATCHLIST)
        if self.trading_mode not in ("demo", "testnet", "live", "paper"):
            raise ValueError("TRADING_MODE must be demo, testnet, live or paper")
        if self.brain not in ("agent", "cli", "mock"):
            raise ValueError("BRAIN must be agent, cli or mock")
        if self.market_source not in ("binance", "sim"):
            raise ValueError("MARKET_SOURCE must be binance or sim")

    # ------------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def uses_exchange_account(self) -> bool:
        """True when orders go to a Binance account (demo, testnet or live)."""
        return self.trading_mode in ("demo", "testnet", "live") and self.market_source == "binance"

    @property
    def binance_url(self) -> str:
        return BINANCE_URLS.get(self.trading_mode, PUBLIC_MARKET_URL)

    @property
    def mode_label(self) -> str:
        if self.market_source == "sim":
            return "SIMULATED MARKET"
        return {"demo": "BINANCE DEMO", "testnet": "BINANCE TESTNET", "live": "LIVE",
                "paper": "PAPER"}[self.trading_mode]

    def runtime_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in RUNTIME_RULES}

    def public_dict(self) -> dict[str, Any]:
        """Everything safe to show on the dashboard (never the keys)."""
        d = asdict(self)
        for secret in ("binance_api_key", "binance_api_secret", "dashboard_password"):
            d[secret] = bool(d[secret])
        d["mode_label"] = self.mode_label
        d["editable"] = list(RUNTIME_RULES)
        return d

    def validate_startup(self) -> list[str]:
        """Problems that must stop the bot from starting."""
        problems = []
        if self.is_live and not self.allow_live:
            problems.append("TRADING_MODE=live needs ALLOW_LIVE=true as well. Tushar.ai is built for demo trading.")
        if self.uses_exchange_account and not (self.binance_api_key and self.binance_api_secret):
            problems.append(
                f"TRADING_MODE={self.trading_mode} needs BINANCE_API_KEY and BINANCE_API_SECRET in .env "
                "(or set TRADING_MODE=paper to trade a simulated account on real prices)."
            )
        return problems


def _coerce(value: Any, typ: type) -> Any:
    if typ is bool:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on", "y"):
            return True
        if s in ("0", "false", "no", "off", "n", ""):
            return False
        raise ValueError(value)
    if typ is list:
        if isinstance(value, (list, tuple)):
            return [str(v).strip().upper() for v in value if str(v).strip()]
        return _split(str(value))
    if typ is int:
        return int(float(value))
    if typ is float:
        return float(value)
    return str(value).strip()


def _clamp(value: Any, lo: Any, hi: Any) -> Any:
    if lo is not None and value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value
