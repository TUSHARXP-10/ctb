"""Tushar Capital: a trading desk of Claudes.

Every scheduled cycle runs as a team:

  phase 1 (parallel)  Chart Analyst | News & Macro Analyst | Derivatives & Flow Analyst
  phase 2             Risk Officer: devil's advocate on every idea, exposure, veto
  phase 3             Head Trader (Tushar): weighs the reports, verifies, decides, executes

Analysts use read-only tools and hand in a structured report through
submit_report. Only the head trader can trade, and only through the same
risk gate. Event-driven cycles (alerts, stop hits) run the head trader alone
for speed unless TEAM_ON_EVENTS is on.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .agent import AgentBrain
from .prompts import ANALYST_PROMPT, PM_TEAM_ADDENDUM, REPORT_SCHEMA, SYSTEM_PROMPT, TEAM, team_section

log = logging.getLogger("tushar.brain.team")

ANALYST_TIMEOUT = 360
TEAM_TRIGGERS = ("scheduled", "startup", "manual")


def is_team_trigger(trigger: str) -> bool:
    t = (trigger or "").lower()
    return any(t.startswith(k) or f"; {k}" in t for k in TEAM_TRIGGERS)


class TeamBrain(AgentBrain):
    name = "agent"

    async def run_cycle(self, briefing: str, cycle_id: int, trigger: str = "", system: str | None = None) -> dict:
        s, eng = self.engine.s, self.engine
        if s.desk_mode != "team" or (not is_team_trigger(trigger) and not s.team_on_events):
            eng.team_mode_note(cycle_id, "solo" if s.desk_mode != "team" else "solo (event-driven cycle)")
            eng.agent_status(cycle_id, "pm", "working")
            res = await super().run_cycle(briefing, cycle_id, trigger)
            eng.agent_status(cycle_id, "pm", "done")
            return res

        eng.team_mode_note(cycle_id, "team")
        started = time.time()
        keys = ["chartist", "macro", "flow"]
        for k in keys:
            eng.agent_status(cycle_id, k, "working")
        eng.agent_status(cycle_id, "risk", "waiting")
        eng.agent_status(cycle_id, "pm", "waiting")
        phase1 = await asyncio.gather(*(self.analyst(k, briefing, cycle_id) for k in keys))
        reports = dict(zip(keys, phase1))

        eng.agent_status(cycle_id, "risk", "working")
        reports["risk"] = await self.analyst("risk", briefing + "\n\n" + team_section(reports), cycle_id)

        eng.agent_status(cycle_id, "pm", "working")
        pm = await super().run_cycle(briefing + "\n\n" + team_section(reports), cycle_id, trigger,
                                     system=SYSTEM_PROMPT + PM_TEAM_ADDENDUM)
        eng.agent_status(cycle_id, "pm", "done")

        analyst_turns = sum(r.get("turns") or 0 for r in reports.values())
        pm["num_turns"] = (pm.get("num_turns") or 0) + analyst_turns
        pm["team"] = {k: {"stance": r.get("stance"), "headline": r.get("headline"), "error": r.get("error")}
                      for k, r in reports.items()}
        pm["team_seconds"] = round(time.time() - started, 1)
        return pm

    async def analyst(self, key: str, briefing: str, cycle_id: int) -> dict:
        from claude_agent_sdk import tool

        s, eng = self.engine.s, self.engine
        meta = TEAM[key]
        box: dict = {}

        @tool("submit_report", "Hand your final report to the head trader. Call it exactly once, at the end.",
              REPORT_SCHEMA)
        async def submit_report(args: dict) -> dict:
            box.update(args)
            return {"content": [{"type": "text", "text": "Report received by the head trader. You are done."}]}

        turns = s.analyst_max_turns
        system = ANALYST_PROMPT.format(name=meta["name"], mission=meta["mission"], turns=max(2, turns - 2))
        prompt = (briefing + f"\n\n# Your task as the {meta['name']}\n{meta['mission']}\n"
                  "End by calling submit_report.")
        t0 = time.time()
        try:
            res = await asyncio.wait_for(
                self.session(prompt, system, meta["tools"], cycle_id=cycle_id, agent=key, max_turns=turns,
                             model=s.analyst_model or None, web=meta["web"], extra=[submit_report],
                             extra_names=["submit_report"]),
                timeout=ANALYST_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            log.warning("%s failed: %s", key, e)
            report = {"error": f"{type(e).__name__}: {e}"[:240], "stance": "neutral", "confidence": 0,
                      "headline": "unavailable", "summary": ""}
            eng.agent_report(cycle_id, key, report)
            return report
        report = dict(box) if box else {
            "stance": "neutral", "confidence": 0.3, "headline": "(no structured report)",
            "summary": (res.get("summary") or "")[:1200]}
        report["turns"] = res.get("num_turns")
        report["seconds"] = round(time.time() - t0, 1)
        report["model"] = res.get("model")
        eng.agent_report(cycle_id, key, report)
        return report
