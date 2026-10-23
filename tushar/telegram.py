"""Telegram: control Tushar.ai from your phone and get every trade as a message.

Setup: talk to @BotFather, create a bot, put its token in TELEGRAM_BOT_TOKEN.
Send /start to your bot: it replies with your chat id. Put that in
TELEGRAM_CHAT_ID and restart. Only that chat can give commands.

Commands: /status /positions /pnl /outlook /team /run /pause /resume
/kill (then /kill confirm) /report, and any other text is a question for Tushar.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

import httpx

log = logging.getLogger("tushar.telegram")

HELP = (
    "<b>Tushar.ai</b>, Claude is the trader\n\n"
    "/status: state, equity, next cycle\n"
    "/positions: open positions\n"
    "/pnl: today and all-time results\n"
    "/outlook: Claude's latest market view\n"
    "/team: the desk's latest analyst reports\n"
    "/run: run a Claude cycle now\n"
    "/pause, /resume: stop or allow new entries\n"
    "/kill: close everything and stop (asks to confirm)\n"
    "/report: write the daily report now\n\n"
    "Anything else you type is a question for Tushar."
)


def esc(x) -> str:
    return html.escape(str(x if x is not None else "-"))


def money(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "-"
    return f"{'+' if v > 0 else '-' if v < 0 else ''}${abs(v):,.2f}"


class TelegramBot:
    def __init__(self, engine, token: str, chat_id: str = "", api: str = "https://api.telegram.org"):
        self.engine = engine
        self.chat_id = str(chat_id or "").strip()
        self.base = f"{api}/bot{token}"
        self.status = "starting"
        self.offset = 0
        self._kill_armed_at = 0.0
        self.client: httpx.AsyncClient | None = None
        self.sent: list[str] = []   # last messages (tests / debugging)

    # ------------------------------------------------------------ transport
    async def call(self, method: str, **params):
        assert self.client is not None
        resp = await self.client.post(f"{self.base}/{method}", json=params, timeout=40)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "telegram error"))
        return data.get("result")

    async def send(self, text: str, chat_id: str | None = None) -> None:
        target = chat_id or self.chat_id
        self.sent.append(text)
        self.sent = self.sent[-50:]
        if not target or self.client is None:
            return
        for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]:
            try:
                await self.call("sendMessage", chat_id=target, text=chunk, parse_mode="HTML",
                                disable_web_page_preview=True)
            except Exception as e:  # noqa: BLE001
                log.warning("telegram send failed: %s", e)

    # ------------------------------------------------------------ loops
    async def run(self) -> None:
        async with httpx.AsyncClient() as client:
            self.client = client
            notifier = asyncio.create_task(self._notify_loop())
            try:
                me = await self.call("getMe")
                self.status = f"@{me.get('username')}" + ("" if self.chat_id else " (send /start to link)")
                if self.chat_id:
                    await self.send(f"Tushar.ai is online. Mode: <b>{esc(self.engine.s.mode_label)}</b>. /help")
            except Exception as e:  # noqa: BLE001
                self.status = f"error: {e}"
                log.warning("telegram: %s", e)
            try:
                while True:
                    try:
                        updates = await self.call("getUpdates", offset=self.offset, timeout=30,
                                                  allowed_updates=["message"])
                    except Exception as e:  # noqa: BLE001
                        log.debug("telegram poll: %s", e)
                        await asyncio.sleep(10)
                        continue
                    for u in updates or []:
                        self.offset = max(self.offset, u["update_id"] + 1)
                        msg = u.get("message") or {}
                        text = (msg.get("text") or "").strip()
                        chat = str((msg.get("chat") or {}).get("id", ""))
                        if text:
                            await self.handle(chat, text)
            finally:
                notifier.cancel()

    async def _notify_loop(self) -> None:
        q = self.engine.bus.subscribe()
        try:
            while True:
                m = await q.get()
                text = self.format_event(m)
                if text:
                    await self.send(text)
        finally:
            self.engine.bus.unsubscribe(q)

    def format_event(self, m: dict) -> str | None:
        d = m.get("data") or {}
        t = m["type"]
        if t == "trade_open":
            tr = d.get("trade") or {}
            return (f"\U0001F7E2 <b>OPENED {esc(tr.get('side', '').upper())} {esc(tr.get('symbol'))}</b>\n"
                    f"entry {esc(tr.get('entry_price'))} · stop {esc(tr.get('stop_loss'))} · target {esc(tr.get('take_profit'))}\n"
                    f"risk {money(tr.get('risk_usd'))} · {esc(tr.get('leverage'))}x · {esc(tr.get('setup'))}\n"
                    f"<i>{esc((tr.get('thesis') or '')[:500])}</i>")
        if t == "trade_close" and d.get("trade"):
            tr = d["trade"]
            pnl = tr.get("pnl") or 0
            icon = "✅" if pnl >= 0 else "\U0001F534"
            r = tr.get("r_multiple")
            return (f"{icon} <b>CLOSED {esc(tr.get('symbol'))} {esc(tr.get('side'))}</b> {money(pnl)}"
                    f"{f' ({r:+.2f}R)' if isinstance(r, (int, float)) else ''}\n"
                    f"exit {esc(tr.get('exit_price'))} · {esc(tr.get('exit_reason'))}")
        if t == "alert_hit":
            return f"\U0001F514 {esc(d.get('text'))}"
        if t == "notice" and d.get("level") == "warn":
            return f"⚠️ {esc(d.get('text'))}"
        if t == "daily_report":
            return f"\U0001F4D3 <b>Daily report {esc(d.get('day'))}</b>\n\n{esc(d.get('text'))}"
        if t == "cycle_end" and self.engine.s.telegram_cycle_summaries and d.get("summary"):
            return f"\U0001F9E0 <b>Cycle #{esc(d.get('cycle_id'))}</b>\n{esc(str(d.get('summary'))[:1500])}"
        if t == "cycle_end" and d.get("status") == "error":
            return f"❌ Cycle #{esc(d.get('cycle_id'))} failed: {esc(d.get('error'))}"
        return None

    # ------------------------------------------------------------ commands
    async def handle(self, chat: str, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if not self.chat_id:
            await self.send(f"Your chat id is <code>{esc(chat)}</code>.\nPut <code>TELEGRAM_CHAT_ID={esc(chat)}</code> "
                            "in Tushar.ai's .env and restart. Until then I won't take commands.", chat_id=chat)
            return
        if chat != self.chat_id:
            await self.send("This bot belongs to someone else.", chat_id=chat)
            return
        eng = self.engine
        try:
            if cmd in ("/start", "/help"):
                await self.send(HELP)
            elif cmd == "/status":
                await self.send(await self.status_text())
            elif cmd == "/positions":
                await self.send(await self.positions_text())
            elif cmd == "/pnl":
                await self.send(await self.pnl_text())
            elif cmd == "/outlook":
                o = eng.desk.outlook
                await self.send(f"\U0001F52D <b>{esc(o.get('bias', '').upper())}</b>, {esc(o.get('regime'))}\n\n"
                                f"{esc(o.get('summary'))}" if o else "No outlook yet.")
            elif cmd == "/team":
                await self.send(self.team_text())
            elif cmd == "/run":
                r = await eng.run_now()
                await self.send("Running a cycle now." if r.get("ok") else esc(r.get("error")))
            elif cmd == "/pause":
                eng.pause()
                await self.send("⏸ Paused: no new entries. Open positions keep their stops.")
            elif cmd == "/resume":
                eng.resume()
                await self.send("▶️ Resumed.")
            elif cmd == "/kill":
                if arg.strip().lower() == "confirm" and time.time() - self._kill_armed_at < 120:
                    closed = await eng.kill()
                    await self.send(f"\U0001F6D1 KILL SWITCH ON. Closed {len(closed)} position(s). "
                                    "Reset it from the dashboard.")
                else:
                    self._kill_armed_at = time.time()
                    await self.send("This closes ALL positions at market and stops trading. "
                                    "Send <code>/kill confirm</code> within 2 minutes.")
            elif cmd == "/report":
                await self.send("Writing the report...")
                await eng.make_daily_report()
            elif cmd.startswith("/"):
                await self.send("Unknown command. /help")
            else:
                await self.send("\U0001F914 Thinking...")
                answer = await eng.chat(text)
                await self.send(esc(answer))
        except Exception as e:  # noqa: BLE001
            log.exception("telegram command failed")
            await self.send(f"Error: {esc(e)}")

    async def status_text(self) -> str:
        eng = self.engine
        st = eng.status()
        acct = await eng.desk.account(max_age=5)
        nxt = st.get("next_cycle_at")
        when = "running now" if st["cycle_running"] else (f"in {max(0, int((nxt - time.time()) / 60))} min" if nxt else "-")
        return (f"<b>Tushar.ai</b> · {esc(st['mode'])}\n"
                f"State: <b>{esc(st['state'].upper())}</b> · desk: {esc(st['desk_mode'])}\n"
                f"Equity: <b>${acct.equity:,.2f}</b> (unrealized {money(acct.unrealized_pnl)})\n"
                f"Open positions: {len(acct.positions)}\n"
                f"Next cycle: {esc(when)} · brain {esc(st['brain'])} ({esc(st['model'])})")

    async def positions_text(self) -> str:
        acct = await self.engine.desk.account(max_age=3)
        if not acct.positions:
            return "No open positions."
        trades = {t["symbol"]: t for t in self.engine.db.open_trades()}
        lines = []
        for p in acct.positions:
            t = trades.get(p.symbol, {})
            lines.append(f"<b>{esc(p.symbol)}</b> {esc(p.side.upper())} {p.qty:g} @ {p.entry_price:g} → {p.mark_price:g}\n"
                         f"  PnL {money(p.unrealized_pnl)} · stop {esc(t.get('stop_loss'))} · target {esc(t.get('take_profit'))}")
        return "\n".join(lines)

    async def pnl_text(self) -> str:
        from .desk import trade_stats, utc_day_start
        db = self.engine.db
        closed = db.recent_trades(500, status="closed")
        today = [t for t in closed if (t["closed_at"] or 0) >= utc_day_start()]
        s_all, s_day = trade_stats(closed), trade_stats(today)
        acct = await self.engine.desk.account(max_age=5)
        sod = self.engine.desk.start_of_day_equity() or acct.equity
        return (f"<b>Today</b>: {money(acct.equity - sod)} equity change · {s_day.get('trades', 0)} closed trades "
                f"· realized {money(s_day.get('total_pnl', 0))}\n"
                f"<b>All time</b>: {s_all.get('trades', 0)} trades · win rate {esc(s_all.get('win_rate', '-'))}% · "
                f"P&L {money(s_all.get('total_pnl', 0))} · PF {esc(s_all.get('profit_factor'))}")

    def team_text(self) -> str:
        view = self.engine.team_view()
        lines = [f"<b>Desk</b> ({esc(view.get('mode'))}, cycle #{esc(view.get('cycle_id'))})"]
        for k in view["order"]:
            a = view["agents"].get(k) or {}
            rep = a.get("report") or {}
            meta = view["meta"][k]
            if rep:
                lines.append(f"{meta['emoji']} <b>{esc(meta['name'])}</b>: {esc(rep.get('stance'))} · {esc(rep.get('headline'))}")
            elif a:
                lines.append(f"{meta['emoji']} <b>{esc(meta['name'])}</b>: {esc(a.get('status'))}")
        return "\n".join(lines)
