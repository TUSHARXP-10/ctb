# Tushar.ai: Claude is the trader

Tushar.ai is a fully automated crypto **futures** trading desk where **Claude makes every decision**.
It trades your **Binance Demo Trading** account, runs on your **Claude Pro/Max subscription** (no API
key, no per-token bill), and shows everything live in a dashboard: Claude's thinking as it happens,
positions, orders, P&L, charts with its entries and exits, news, the setup scanner, the Time Machine
replays, its memory of lessons learned, and its market outlook.

![Tushar.ai dashboard](docs/screenshot.png)

> The screenshot was taken in offline test mode (simulated prices), with real Claude making the decisions.

---

## What Claude does, every cycle

```
           every 15 min, or woken by: a price alert, a big move, a stop/target hit
                                        |
      briefing: account, positions, closed trades, market overview, scanner hits,
                news + Fear & Greed, its memory, its track record, risk limits
                                        |
                                     CLAUDE
   1. reviews closed trades and writes lessons into its memory
   2. manages open positions: trail stops, take partials, exit broken theses
   3. hunts: scan_market -> analyze_symbol (15m/1h/4h/1d, funding, OI, order book, news)
   4. plan_trade: sizing + every risk limit + Time Machine (10,000 replays)
   5. open_position (only if the risk gate says PASS) with exchange stop loss + take profit
   6. set_alert on key levels, publish_outlook, pick when to look again
```

Claude works through 17 tools (`tushar/brain/tools.py`). The tools are the only way it can touch the
account, and every entry goes through a **risk gate that Claude cannot override**.

### Ideas borrowed from day-trader-skill

[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT) is a set of stock day-trading
skills. Tushar.ai adapts its best ideas to 24/7 crypto futures and lets Claude run them on its own:

| Idea | In Tushar.ai |
| --- | --- |
| Chart scanner | `scanner.py`: breakouts/breakdowns, bull/bear flags, trend pullbacks, EMA50 reclaim/loss, RSI bounces, squeezes, volume spikes, funding extremes across the top USDT perpetuals |
| Exit planner (stop + target before entry) | every `open_position` requires a stop and a target; stops round away from the entry |
| **Time Machine** | `timemachine.py`: replays the trade 10,000 times from the coin's own 5m candles (same time of day), re-runs the same stop/target distances on the real history, and gives a verdict that weighs the target's R. It can veto entries with negative expectancy after fees |
| Budget (size from max loss) | quantity = risk $ / stop distance, capped by margin limits |
| Risk manager PASS/BLOCK | 17 checks: kill switch, daily loss, open positions, duplicates, reward:risk, stop distance, cooldown after a loss, leverage vs liquidation, minimum size, margin, Time Machine |
| Price alerts | Claude sets alerts that wake it for an extra cycle |
| News monitor | RSS from CoinDesk, Cointelegraph, Decrypt, The Block, Bitcoin Magazine, CryptoSlate, CNBC Economy, plus the Fear & Greed index, tagged by topic and coin |

The one thing intentionally left out is the "you approve every trade" step: here Claude trades on its
own, inside hard limits.

---

## Quick start

You need Python 3.10+ and a Claude Pro or Max subscription.

### 1. Get Binance Demo Trading API keys

1. Go to **https://demo.binance.com** and log in with your Binance account.
2. Open **API Management** and create an API key (system-generated / HMAC).
3. Copy the **API Key** and **Secret Key**. Demo trading uses fake money; the keys only work there.

> Keep the keys on your own machine in `.env`. Don't paste them into chats or commit them.

### 2. Log in to Claude with your subscription (this is the "no API" part)

Tushar.ai drives **Claude Code** through the Claude Agent SDK, which uses your Pro/Max login.

```bash
# install Claude Code (any one of these)
curl -fsSL https://claude.ai/install.sh | bash        # macOS / Linux
npm install -g @anthropic-ai/claude-code              # anywhere with Node.js

claude          # opens Claude Code: type /login and sign in with your Pro/Max account, then /exit
```

On a server or in Docker, run `claude setup-token` on your computer and put the token in `.env` as
`CLAUDE_CODE_OAUTH_TOKEN=...`.

If `ANTHROPIC_API_KEY` is set in your environment, Tushar.ai removes it at startup so you are never
billed per token (`CLAUDE_USE_SUBSCRIPTION=true`).

### 3. Install and configure

```bash
git clone https://github.com/TUSHARXP-10/ctb.git tushar-ai && cd tushar-ai
./run.sh            # first run creates .venv and .env, then stops
nano .env           # paste BINANCE_API_KEY and BINANCE_API_SECRET (TRADING_MODE=demo)
```

Windows: use `run.bat` instead of `./run.sh`.

### 4. Check everything

```bash
./run.sh doctor
```

It checks Binance market data, your demo account balance, the stop/target order API, the news feeds,
and that Claude answers **on your subscription** (`apiKeySource=none`).

### 5. Start trading

```bash
./run.sh
```

Open **http://127.0.0.1:8000**. The first Claude cycle starts a few seconds after launch.

No Binance keys yet? Set `TRADING_MODE=paper` to trade a simulated $10,000 account on real Binance prices.

---

## The dashboard

| Panel | What it shows |
| --- | --- |
| Top bar | mode (DEMO/PAPER/LIVE), trading state, "Claude is thinking..." indicator, countdown to the next cycle, subscription usage meter, **Run now / Pause / Kill switch / Settings** |
| KPIs | equity, today's P&L, unrealized P&L, available margin, win rate, total P&L, money at risk at the stops |
| Chart | candles (5m-1D) with EMA20/50, Claude's entries and exits as markers, live entry/stop/target lines |
| Claude's mind | live stream of Claude's reasoning, every tool call and result, trades, alerts |
| Outlook | Claude's market regime, bias, summary and watchlist with levels |
| Positions / Orders / Journal / Cycles / Alerts | the book; the journal has each trade's thesis, Claude's own review and its Time Machine verdict; a cycle opens the full replay, including the exact briefing Claude received |
| Market watch, Scanner, News | live prices and funding, current setups, headlines with tags and the Fear & Greed gauge |
| Time Machine | odds of target vs stop, average R after fees, and a fan chart of 10,000 replays for the latest plan |
| Equity curve, Memory, Performance by setup | how the account and Claude's lessons evolve |
| Ask Tushar | chat with your trader ("why did you short ETH?"), backed by the same tools (read-only by default) |

All risk limits, the watchlist, the model and the schedule can be changed live from **Settings**.

---

## Safety rails (enforced in code)

- **Demo first.** Live trading needs `TRADING_MODE=live` **and** `ALLOW_LIVE=true`.
- **Every position has a stop loss and take profit on the exchange** (Binance Algo Order API). If the
  exchange orders ever go missing, Tushar.ai re-places them, and enforces the levels itself in the meantime.
- **Size from the loss at the stop**: max `RISK_PER_TRADE_PCT` of equity per trade (default 1.5%).
- **Daily loss limit** (default 6%): new entries stop until 00:00 UTC.
- Max open positions, max entries per day, max margin per position, max leverage, isolated margin,
  leverage lowered automatically so liquidation sits beyond the stop.
- **Stops can only be tightened**, never widened (configurable).
- **Cooldown** on a symbol after a loss.
- **Time Machine gate**: blocks entries whose replays show negative expectancy after fees.
- **Kill switch**: one click stops Claude, closes everything at market and blocks new trades.
- The dashboard binds to `127.0.0.1` by default; set `DASHBOARD_PASSWORD` before exposing it.

---

## Configuration

Everything lives in `.env` (see `.env.example`). The main settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `TRADING_MODE` | `demo` | `demo`, `testnet`, `paper`, `live` |
| `BRAIN` | `agent` | `agent` (Claude with live tools), `cli` (`claude -p`, single pass), `mock` (rules, for testing) |
| `CLAUDE_MODEL` | `sonnet` | `sonnet`, `opus`, `haiku` or a full model id |
| `CLAUDE_ALLOW_WEB` | `true` | Claude may use web search for news |
| `CYCLE_MINUTES` | `15` | regular cycle; Claude can ask for sooner or later each time |
| `WATCHLIST` | BTC, ETH, SOL, BNB, XRP, DOGE | always in the briefing |
| `SCAN_TOP_N` | `40` | scanner universe (most traded USDT perpetuals) |
| `RISK_PER_TRADE_PCT` | `1.5` | max loss at the stop per trade, % of equity |
| `MAX_LEVERAGE` / `DEFAULT_LEVERAGE` | `10` / `5` | |
| `MAX_OPEN_POSITIONS` | `4` | |
| `MAX_DAILY_LOSS_PCT` | `6` | halts new entries for the day |
| `MIN_REWARD_RISK` | `1.5` | |
| `TM_GATE` / `TM_MIN_AVG_R` | `true` / `-0.15` | Time Machine veto threshold (average R after fees) |
| `DASHBOARD_PASSWORD` | empty | required if the dashboard is reachable from other devices |

### Claude usage

Each cycle is one Claude Code session (typically 5-20 tool calls). At 15-minute cycles that is about 96
sessions a day. **Max** plans handle this comfortably with `sonnet`; on **Pro**, use `CYCLE_MINUTES=30`
or more, or `CLAUDE_MODEL=haiku`. When you hit the usage window, Tushar.ai waits for the reset and shows
it in the top bar. Open positions stay protected by their exchange stops in the meantime.

---

## Running 24/7

The bot only trades while it is running. For round-the-clock trading, use a small VPS or an always-on PC.

**Docker**

```bash
cp .env.example .env    # fill in Binance keys and CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`)
docker compose up -d --build
docker compose logs -f
```

**Plain server**: `./run.sh` inside `tmux`/`screen`, or a systemd service.

**Phone access**: keep the dashboard on `127.0.0.1` and use an SSH tunnel
(`ssh -L 8000:127.0.0.1:8000 you@server`) or Tailscale. If you open the port, set `DASHBOARD_PASSWORD`.

---

## Commands

```bash
./run.sh            # bot + dashboard
./run.sh doctor     # check Binance, news and the Claude login
./run.sh cycle      # run one Claude cycle in the terminal and print its thinking
```

Offline test mode (no network, synthetic prices, rule-based brain):

```bash
MARKET_SOURCE=sim TRADING_MODE=paper BRAIN=mock python -m tushar
```

Tests (offline, about 15 seconds):

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## Project layout

```
tushar/
  engine.py         loops: prices, position monitor, news, scanner, Claude scheduler
  desk.py           every action Claude can take (open/close/stops/alerts/memory/outlook)
  risk.py           position sizing + PASS/BLOCK risk gate
  timemachine.py    Monte Carlo + history replay of a trade plan
  scanner.py        setup scanner
  market.py         cached market data and multi-timeframe snapshots
  news.py           RSS + Fear & Greed
  indicators.py     EMA, RSI, ATR, MACD, Bollinger, ADX, pivots
  brain/            agent.py (Claude Agent SDK), cli.py (claude -p), mock.py, prompts.py, tools.py
  exchange/         binance.py (demo/testnet/live), paper.py (simulated account), sim.py (synthetic market)
  server.py         FastAPI + WebSocket
  web/              the dashboard (no build step)
tests/              57 offline tests
```

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `doctor` says Claude failed | run `claude` and `/login` with your Pro/Max account, or set `CLAUDE_CODE_OAUTH_TOKEN` |
| `apiKeySource` is `ANTHROPIC_API_KEY` | remove the key from your environment (or keep `CLAUDE_USE_SUBSCRIPTION=true`) |
| `Invalid API-key, IP, or permissions` | make sure the keys come from demo.binance.com and `TRADING_MODE=demo` |
| HTTP 451 from Binance | Binance is not available from your location or server region |
| Stop/target errors | `doctor` shows whether the Algo Order API is available; Tushar.ai falls back automatically and enforces stops itself |
| News panel empty | your network blocks the RSS feeds; Claude can still use web search |

---

**Disclaimer.** Tushar.ai is software for experimenting with AI-driven trading on a demo account. It is
not financial advice. Futures with leverage can lose money fast, and an AI trader can be wrong.
Simulated odds are not predictions. If you ever switch to real money, that is your decision and your risk.
