"""Claude through the `claude -p` command line (fallback brain).

No live tool calls: Tushar.ai pre-computes the analysis, Claude answers with a
structured JSON decision, and the desk executes it through the same risk gate.
Uses whatever account the `claude` command is logged into (your subscription).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .prompts import SYSTEM_PROMPT

log = logging.getLogger("tushar.brain.cli")

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string", "description": "your reasoning, concise"},
        "reviews": {"type": "array", "items": {"type": "object", "properties": {
            "trade_id": {"type": "integer"}, "lesson": {"type": "string"}, "grade": {"type": "string"}},
            "required": ["trade_id", "lesson"]}},
        "memories": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "kind": {"type": "string"}, "importance": {"type": "integer"},
            "symbol": {"type": "string"}}, "required": ["text"]}},
        "actions": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["open", "close", "update_protection", "set_alert", "cancel_alert"]},
            "symbol": {"type": "string"}, "side": {"type": "string", "enum": ["long", "short"]},
            "stop_loss": {"type": "number"}, "take_profit": {"type": "number"}, "risk_pct": {"type": "number"},
            "leverage": {"type": "integer"}, "confidence": {"type": "number"}, "setup": {"type": "string"},
            "thesis": {"type": "string"}, "fraction": {"type": "number"}, "reason": {"type": "string"},
            "condition": {"type": "string", "enum": ["above", "below"]}, "price": {"type": "number"},
            "note": {"type": "string"}, "alert_id": {"type": "integer"}}, "required": ["type"]}},
        "outlook": {"type": "object", "properties": {
            "regime": {"type": "string"}, "bias": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
            "summary": {"type": "string"}, "next_check_minutes": {"type": "integer"},
            "watch": {"type": "array", "items": {"type": "object", "properties": {
                "symbol": {"type": "string"}, "bias": {"type": "string"}, "note": {"type": "string"}}}}},
            "required": ["regime", "bias", "summary"]},
    },
    "required": ["analysis", "actions", "outlook"],
}

CLI_INSTRUCTIONS = """
# This run has no live tools
All the data you get is in the briefing below, including deep analysis and Time Machine replays for the
main candidates. Decide, then answer ONLY with the JSON object described by the schema:
- "reviews": one per closed trade listed for review.
- "actions": in order. "open" needs symbol, side, stop_loss, take_profit, thesis (risk_pct, leverage,
  confidence, setup optional). Each open passes the same risk gate and Time Machine check; a BLOCK is final.
  "close" needs symbol, reason (fraction optional). "update_protection" needs symbol and stop_loss and/or
  take_profit. "set_alert" needs symbol, condition, price, note.
- "outlook": always.
An empty "actions" list is a perfectly good answer.
"""


def extract_json(text: str) -> dict | None:
    """Pull the decision object out of a reply that may have prose or code fences around it."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


class CliBrain:
    name = "cli"

    def __init__(self, engine):
        self.engine = engine
        self.workspace = Path(engine.s.data_dir) / "claude_workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _binary(self) -> str:
        path = self.engine.s.claude_cli_path or shutil.which("claude")
        if not path:
            try:  # the Agent SDK ships its own copy of Claude Code
                import claude_agent_sdk
                bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
                if bundled.exists():
                    path = str(bundled)
            except ImportError:
                pass
        if not path:
            raise RuntimeError("`claude` command not found. Install Claude Code and run `claude` once to log in.")
        return path

    async def _run(self, prompt: str, system: str, schema: dict | None, timeout: float = 900) -> dict:
        s = self.engine.s
        args = [self._binary(), "-p", "--output-format", "json", "--append-system-prompt", system,
                "--strict-mcp-config", "--permission-mode", "dontAsk"]
        if s.claude_model:
            args += ["--model", s.claude_model]
        if s.claude_allow_web:
            args += ["--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch", "WebFetch"]
        else:
            args += ["--tools", ""]
        if schema:
            args += ["--json-schema", json.dumps(schema)]
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self.workspace), env=dict(os.environ))
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("claude -p timed out")
        if proc.returncode != 0 and not out:
            raise RuntimeError(f"claude -p failed ({proc.returncode}): {err.decode()[-500:]}")
        try:
            return json.loads(out.decode())
        except ValueError:
            return {"result": out.decode(), "is_error": proc.returncode != 0}

    async def _deep_analysis(self) -> str:
        """Pre-compute what an agent would have looked up with tools."""
        desk = self.engine.desk
        scan = desk.scanner.last.get("hits") or []
        positions = [p.symbol for p in (await desk.account()).positions]
        cands: list[str] = []
        for sym in positions + ["BTCUSDT"] + [h["symbol"] for h in scan if h["direction"] in ("long", "short")]:
            if sym not in cands:
                cands.append(sym)
        cands = cands[:5]
        parts = ["## Deep analysis of the main candidates"]
        for sym in cands:
            try:
                snap = await desk.analyze_symbol(sym)
                parts.append(f"### {sym}\n" + json.dumps(snap, default=str))
            except Exception as e:  # noqa: BLE001
                parts.append(f"### {sym}\nunavailable: {e}")
        # Time Machine for the strongest directional scanner hits, ATR-based plan.
        tm_parts = []
        for h in [h for h in scan if h["direction"] in ("long", "short") and h["symbol"] not in positions][:3]:
            try:
                snap = await desk.market.snapshot(h["symbol"])
                atr = snap["1h"].get("atr") or 0
                price = snap["price"]
                if not atr:
                    continue
                stop = price - 1.5 * atr if h["direction"] == "long" else price + 1.5 * atr
                target = price + 3 * atr if h["direction"] == "long" else price - 3 * atr
                res = await desk.run_time_machine(h["symbol"], h["direction"], stop, target)
                if res.get("ok"):
                    tm_parts.append(json.dumps({"symbol": h["symbol"], "side": h["direction"], "plan": res["plan"],
                                                "verdict": res["verdict"],
                                                "mc": {k: v for k, v in res["mc"].items() if k != "fan"},
                                                "history": res["history"]}, default=str))
            except Exception as e:  # noqa: BLE001
                log.debug("tm for %s failed: %s", h["symbol"], e)
        if tm_parts:
            parts += ["## Time Machine on 1.5 ATR stop / 3 ATR target plans for top scanner hits"] + tm_parts
        return "\n".join(parts)

    async def run_cycle(self, briefing: str, cycle_id: int) -> dict:
        engine, desk, bus = self.engine, self.engine.desk, self.engine.bus
        prompt = briefing + "\n\n" + await self._deep_analysis()
        bus.publish("thought", {"cycle_id": cycle_id, "text": "Thinking (claude -p, single pass)..."})
        raw = await self._run(prompt, SYSTEM_PROMPT + CLI_INSTRUCTIONS, DECISION_SCHEMA)
        decision = raw.get("structured_output") or extract_json(raw.get("result") or "")
        result: dict[str, Any] = {
            "num_turns": raw.get("num_turns"), "duration_ms": raw.get("duration_ms"),
            "cost_usd": raw.get("total_cost_usd"), "is_error": raw.get("is_error"),
            "input_tokens": (raw.get("usage") or {}).get("input_tokens"),
            "output_tokens": (raw.get("usage") or {}).get("output_tokens"),
        }
        if not decision:
            result["summary"] = f"Could not parse a decision: {(raw.get('result') or '')[:300]}"
            result["is_error"] = True
            return result
        engine.db.add_event(cycle_id, "decision", decision)
        if decision.get("analysis"):
            bus.publish("thought", {"cycle_id": cycle_id, "text": decision["analysis"]})
            engine.db.add_event(cycle_id, "thought", {"text": decision["analysis"]})

        async def do(name: str, coro, inp: dict | None = None) -> None:
            bus.publish("tool_call", {"cycle_id": cycle_id, "name": name, "input": inp or {}})
            try:
                res = await coro
            except Exception as e:  # noqa: BLE001
                res = {"status": "ERROR", "error": str(e)}
            bus.publish("tool_result", {"cycle_id": cycle_id, "text": json.dumps(res, default=str)[:1200]})
            engine.db.add_event(cycle_id, "tool_result", {"name": name, "result": res})

        for rv in decision.get("reviews") or []:
            await do("review_trade", desk.review_trade(rv.get("trade_id"), rv.get("lesson", ""), rv.get("grade", "")), rv)
        for m in decision.get("memories") or []:
            await do("save_memory", desk.save_memory(m.get("text", ""), m.get("kind", "lesson"), m.get("symbol"),
                                                     m.get("importance", 3)), m)
        for a in decision.get("actions") or []:
            t = a.get("type")
            if t == "open":
                await do("open_position", desk.open_position(
                    a.get("symbol", ""), a.get("side", ""), a.get("stop_loss") or 0, a.get("take_profit") or 0,
                    a.get("risk_pct"), a.get("leverage"), a.get("confidence"), a.get("setup", ""), a.get("thesis", "")), a)
            elif t == "close":
                await do("close_position", desk.close_position(a.get("symbol", ""), a.get("fraction") or 1.0,
                                                               a.get("reason", "")), a)
            elif t == "update_protection":
                await do("update_protection", desk.update_protection(a.get("symbol", ""), a.get("stop_loss"),
                                                                     a.get("take_profit"), a.get("reason", "")), a)
            elif t == "set_alert":
                await do("set_alert", desk.set_alert(a.get("symbol", ""), a.get("condition", "above"),
                                                     a.get("price") or 0, a.get("note", "")), a)
            elif t == "cancel_alert" and a.get("alert_id"):
                await do("cancel_alert", desk.cancel_alert(a["alert_id"]), a)
        o = decision.get("outlook") or {}
        if o:
            await do("publish_outlook", desk.publish_outlook(o.get("regime", ""), o.get("bias", "neutral"),
                                                             o.get("summary", ""), o.get("watch"),
                                                             o.get("next_check_minutes")), o)
        result["summary"] = (o.get("summary") or decision.get("analysis") or "")[:1500]
        return result

    async def chat(self, message: str, history: list[dict]) -> str:
        desk = self.engine.desk
        context = {"portfolio": await desk.get_portfolio(), "outlook": desk.outlook,
                   "recent_trades": (await desk.get_trade_journal(8))["trades"]}
        convo = "\n".join(f"{m['role'].upper()}: {m['text']}" for m in history[-10:])
        prompt = (f"Current desk state:\n{json.dumps(context, default=str)}\n\n"
                  + (f"Earlier in this chat:\n{convo}\n\n" if convo else "") + f"OWNER: {message}")
        system = ("You are TUSHAR.AI, the autonomous crypto futures trader running this account, chatting with "
                  "the owner. Be direct and specific. You cannot place trades from this chat.")
        raw = await self._run(prompt, system, None, timeout=300)
        return (raw.get("result") or "(no answer)").strip()
