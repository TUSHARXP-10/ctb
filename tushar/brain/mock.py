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

    async def run_cycle(self, briefing: str, cycle_id: int) -> dict:
        desk, bus = self.engine.desk, self.engine.bus
        calls = []

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
        return {"summary": summary, "num_turns": len(calls), "duration_ms": 0, "tool_calls": calls}

    async def chat(self, message: str, history: list[dict]) -> str:
        pf = await self.engine.desk.get_portfolio()
        return (f"[mock brain] Equity ${pf['equity']:,} with {len(pf['positions'])} open position(s). "
                f"Set BRAIN=agent to talk to Claude. You said: {message[:200]}")

