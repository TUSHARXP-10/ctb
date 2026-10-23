"""Mock brain: a simple rule-based trader for tests and offline demos (BRAIN=mock).

It exercises the whole pipeline (scanner -> plan -> time machine -> risk gate
-> orders -> stops -> outlook) without calling Claude. Its thoughts are
labelled [mock] on the dashboard.
"""
from __future__ import annotations

import json


class MockBrain:
    name = "mock"

    def __init__(self, engine):
        self.engine = engine

    async def _mock_team(self, cycle_id: int) -> None:
        """Rule-based stand-ins for the analysts so the War Room works offline."""
        eng, desk = self.engine, self.engine.desk
        for k in ("chartist", "macro", "flow"):
            eng.agent_status(cycle_id, k, "working")
        scan = await desk.scan_market()
        hits = [h for h in scan["hits"] if h["direction"] in ("long", "short")]
        longs = sum(h["direction"] == "long" for h in hits)
        shorts = len(hits) - longs
        stance = "bullish" if longs > shorts else "bearish" if shorts > longs else "neutral"
        eng.agent_report(cycle_id, "chartist", {
            "stance": stance, "confidence": 0.5, "headline": f"[mock] {len(hits)} directional setups ({longs}L/{shorts}S)",
            "summary": "[mock] Rule-based chart read from the scanner.",
            "ideas": [{"symbol": h["symbol"], "side": h["direction"], "setup": h["setup"], "conviction": min(5, h["score"])}
                      for h in hits[:3]]})
        news = await desk.get_news(hours=12)
        fng = news.get("fear_greed") or {}
        eng.agent_report(cycle_id, "macro", {
            "stance": "neutral", "confidence": 0.3,
            "headline": f"[mock] {len(news['headlines'])} headlines, Fear & Greed {fng.get('value', 'n/a')}",
            "summary": "[mock] No language model: headlines counted, not read."})
        der = await desk.get_derivatives()
        liq = der.get("liquidations_60m") or {}
        eng.agent_report(cycle_id, "flow", {
            "stance": "neutral", "confidence": 0.4,
            "headline": f"[mock] avg funding {der.get('avg_funding_pct_per_8h')}%, liquidations {liq.get('dominant', 'n/a')}",
            "summary": "[mock] Positioning summary from funding and the liquidation radar."})
        eng.agent_status(cycle_id, "risk", "working")
        corr = await desk.get_correlations()
        ex = corr.get("exposure", {})
        eng.agent_report(cycle_id, "risk", {
            "stance": "neutral", "confidence": 0.5,
            "headline": f"[mock] BTC-beta net exposure {ex.get('btc_beta_net_x_equity', 0)}x equity",
            "summary": "[mock] Exposure and correlation check.", "max_risk_pct": self.engine.s.risk_per_trade_pct,
            "warnings": [f"{p['a']}/{p['b']} correlated {p['corr']}" for p in corr.get("highly_correlated_pairs", [])[:3]]})

    async def run_cycle(self, briefing: str, cycle_id: int, trigger: str = "") -> dict:
        desk, bus = self.engine.desk, self.engine.bus
        calls = []
        from .team import is_team_trigger
        if self.engine.s.desk_mode == "team" and is_team_trigger(trigger):
            self.engine.team_mode_note(cycle_id, "team (mock)")
            await self._mock_team(cycle_id)
        else:
            self.engine.team_mode_note(cycle_id, "solo (mock)")
        self.engine.agent_status(cycle_id, "pm", "working")

        def think(text: str) -> None:
            bus.publish("thought", {"cycle_id": cycle_id, "text": f"[mock] {text}"})
            self.engine.db.add_event(cycle_id, "thought", {"text": f"[mock] {text}"})

        async def call(name: str, coro):
            bus.publish("tool_call", {"cycle_id": cycle_id, "name": name, "input": {}})
            res = await coro
            calls.append(name)
            bus.publish("tool_result", {"cycle_id": cycle_id, "text": json.dumps(res, default=str)[:800]})
            return res

        think("Rule-based stand-in for Claude. Checking the book first.")
        for t in self.engine.db.unreviewed_closed()[:5]:
            await call("review_trade", desk.review_trade(t["id"], f"Closed {t['exit_reason']} at {t['r_multiple']}R.", "C"))
        pf = await call("get_portfolio", desk.get_portfolio())
        for p in pf["positions"]:
            if p.get("r_now") is not None and p["r_now"] >= 1.0 and p.get("stop_loss"):
                entry = p["entry"]
                better = (p["side"] == "long" and p["stop_loss"] < entry) or (p["side"] == "short" and p["stop_loss"] > entry)
                if better:
                    think(f"{p['symbol']} is +{p['r_now']}R: moving the stop to break-even.")
                    await call("update_protection", desk.update_protection(p["symbol"], stop_loss=entry,
                                                                           reason="mock: +1R, stop to break-even"))
        scan = await call("scan_market", desk.scan_market())
        opened = None
        held = {p["symbol"] for p in pf["positions"]}
        for h in scan["hits"]:
            if h["direction"] not in ("long", "short") or h["score"] < 3 or h["symbol"] in held:
                continue
            snap = await desk.market.snapshot(h["symbol"])
            atr, price = snap["1h"].get("atr"), snap["price"]
            if not atr:
                continue
            long_ = h["direction"] == "long"
            stop = price - 1.5 * atr if long_ else price + 1.5 * atr
            target = price + 3.3 * atr if long_ else price - 3.3 * atr
            plan = await call("plan_trade", desk.plan_trade(h["symbol"], h["direction"], stop, target))
            think(f"{h['symbol']} {h['setup']}: plan {plan['status']}"
                  + (f" ({'; '.join(plan['blocks'])})" if plan["blocks"] else ""))
            if plan["status"] == "PASS":
                opened = await call("open_position", desk.open_position(
                    h["symbol"], h["direction"], stop, target, confidence=0.5, setup=h["setup"],
                    thesis=f"[mock] {h['description']}; 1.5 ATR stop, 2.2R target."))
                break
        summary = (f"Opened {opened['symbol']} {opened['side']}." if opened and opened.get("status") == "FILLED"
                   else "No setup passed the gate; waiting.")
        await call("publish_outlook", desk.publish_outlook("mock regime", "neutral", f"[mock] {summary}",
                                                           [{"symbol": "BTCUSDT", "bias": "neutral", "note": "mock"}]))
        think(summary)
        self.engine.agent_status(cycle_id, "pm", "done")
        return {"summary": summary, "num_turns": len(calls), "duration_ms": 0, "tool_calls": calls}

    async def daily_report(self, context: str) -> str:
        return "[mock brain] Daily report:\n" + context[:1500]

    async def chat(self, message: str, history: list[dict]) -> str:
        pf = await self.engine.desk.get_portfolio()
        return (f"[mock brain] Equity ${pf['equity']:,} with {len(pf['positions'])} open position(s). "
                f"Set BRAIN=agent to talk to Claude. You said: {message[:200]}")

