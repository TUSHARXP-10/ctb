"""Claude as an agent: the Claude Agent SDK drives Claude Code with Tushar.ai's tools.

Runs on your Claude subscription (Pro/Max login or CLAUDE_CODE_OAUTH_TOKEN),
not an API key. Claude calls the desk's tools in-process through an SDK MCP
server; every text block, tool call and result is streamed to the dashboard,
tagged with which agent produced it.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .prompts import CHAT_PROMPT, DAILY_PROMPT, SYSTEM_PROMPT
from .tools import TOOLS, call_tool, tool_names

log = logging.getLogger("tushar.brain.agent")

WEB_TOOLS = ["WebSearch", "WebFetch"]


def _short(obj: Any, limit: int = 600) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return text if len(text) <= limit else text[:limit] + " ..."


class AgentBrain:
    name = "agent"

    def __init__(self, engine):
        self.engine = engine
        import claude_agent_sdk  # noqa: F401  (fail early if missing)
        self.workspace = Path(engine.s.data_dir) / "claude_workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ plumbing
    def _server(self, names: list[str], extra: list | None = None, agent: str = "pm"):
        from claude_agent_sdk import create_sdk_mcp_server, tool

        desk, bus = self.engine.desk, self.engine.bus
        tools = list(extra or [])
        for name in names:
            desc, schema, _, _ = TOOLS[name]

            def make(n: str):
                async def handler(args: dict) -> dict:
                    try:
                        result = await call_tool(desk, n, args)
                        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
                    except Exception as e:  # noqa: BLE001
                        log.exception("tool %s failed", n)
                        bus.publish("error", {"cycle_id": desk.cycle_id, "agent": agent, "text": f"tool {n} failed: {e}"})
                        return {"content": [{"type": "text", "text": f"ERROR: {e}"}], "is_error": True}
                return handler

            tools.append(tool(name, desc, schema)(make(name)))
        return create_sdk_mcp_server("tushar", "1.0.0", tools)

    def _options(self, system_prompt: str, names: list[str], max_turns: int, model: str | None = None,
                 web: bool | None = None, extra: list | None = None, extra_names: list[str] | None = None,
                 agent: str = "pm"):
        from claude_agent_sdk import ClaudeAgentOptions

        s = self.engine.s
        use_web = s.claude_allow_web if web is None else (web and s.claude_allow_web)
        webt = WEB_TOOLS if use_web else []
        allowed = [f"mcp__tushar__{n}" for n in names + (extra_names or [])] + webt
        kwargs: dict[str, Any] = dict(
            system_prompt=system_prompt,
            mcp_servers={"tushar": self._server(names, extra, agent)},
            tools=list(webt),           # built-in tools: only web search/fetch (or none)
            allowed_tools=allowed,
            permission_mode="dontAsk",  # anything not pre-approved is denied, never prompted
            max_turns=max_turns,
            model=(model or s.claude_model) or None,
            cwd=str(self.workspace),
            setting_sources=[],         # ignore your personal Claude Code settings / CLAUDE.md
            stderr=lambda line: log.debug("claude: %s", line),
        )
        if s.claude_cli_path:
            kwargs["cli_path"] = s.claude_cli_path
        if s.claude_effort in ("low", "medium", "high", "xhigh", "max"):
            kwargs["effort"] = s.claude_effort
        return ClaudeAgentOptions(**kwargs)

    async def session(self, prompt: str, system: str, names: list[str], *, cycle_id: int | None, agent: str = "pm",
                      max_turns: int = 40, model: str | None = None, web: bool | None = None,
                      extra: list | None = None, extra_names: list[str] | None = None,
                      channel: str = "mind") -> dict:
        """Run one Claude Code session and stream it to the dashboard."""
        from claude_agent_sdk import ClaudeSDKClient

        opts = self._options(system, names, max_turns, model, web, extra, extra_names, agent)
        result: dict[str, Any] = {"texts": [], "agent": agent}
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                self._handle(msg, cycle_id, result, channel=channel, agent=agent)
        result["summary"] = (result.get("result") or "\n".join(result["texts"][-2:]) or "").strip()
        return result

    # ------------------------------------------------------------ cycle
    async def run_cycle(self, briefing: str, cycle_id: int, trigger: str = "", system: str | None = None) -> dict:
        return await self.session(briefing, system or SYSTEM_PROMPT, tool_names(True), cycle_id=cycle_id,
                                  agent="pm", max_turns=self.engine.s.claude_max_turns)

    def _handle(self, msg, cycle_id: int | None, result: dict, channel: str = "mind", agent: str = "pm") -> None:
        from claude_agent_sdk import (AssistantMessage, RateLimitEvent, ResultMessage, SystemMessage, TextBlock,
                                      ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage)

        bus, db = self.engine.bus, self.engine.db

        def emit(kind: str, data: dict) -> None:
            data = {"cycle_id": cycle_id, "agent": agent, **data}
            if channel == "chat":
                bus.publish("chat_event", {"kind": kind, **data})
            elif channel == "quiet":
                return
            else:
                bus.publish(kind, data)
                db.add_event(cycle_id, kind, data)

        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                info = {"model": msg.data.get("model"), "auth": msg.data.get("apiKeySource"),
                        "claude_code_version": msg.data.get("claude_code_version")}
                result["init"] = info
                if agent == "pm":
                    self.engine.note_claude_session(info)
        elif isinstance(msg, AssistantMessage):
            if getattr(msg, "error", None):
                result["assistant_error"] = str(msg.error)
            result["model"] = msg.model
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    result["texts"].append(block.text)
                    emit("thought", {"text": block.text})
                elif isinstance(block, ThinkingBlock) and getattr(block, "thinking", "").strip():
                    emit("thinking", {"text": block.thinking[:4000]})
                elif isinstance(block, ToolUseBlock):
                    name = block.name.replace("mcp__tushar__", "")
                    result.setdefault("tool_calls", []).append(name)
                    emit("tool_call", {"id": block.id, "name": name, "input": block.input})
        elif isinstance(msg, UserMessage):
            content = msg.content if isinstance(msg.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    text = block.content if isinstance(block.content, str) else \
                        " ".join(c.get("text", "") for c in (block.content or []) if isinstance(c, dict))
                    emit("tool_result", {"id": block.tool_use_id, "text": _short(text, 1200),
                                         "is_error": bool(block.is_error)})
        elif isinstance(msg, ResultMessage):
            usage = msg.usage or {}
            result.update({
                "result": msg.result, "is_error": msg.is_error, "num_turns": msg.num_turns,
                "duration_ms": msg.duration_ms, "cost_usd": msg.total_cost_usd, "subtype": msg.subtype,
                "input_tokens": (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0),
                "output_tokens": usage.get("output_tokens") or 0,
                "errors": getattr(msg, "errors", None),
            })
        elif isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            self.engine.note_usage({"status": info.status, "utilization": info.utilization,
                                    "resets_at": info.resets_at, "type": info.rate_limit_type})

    # ------------------------------------------------------------ chat & reports
    async def chat(self, message: str, history: list[dict]) -> str:
        can_trade = self.engine.s.chat_can_trade
        note = ("You may trade if the owner explicitly asks; the same risk gate applies." if can_trade
                else "You cannot place or change trades from chat; say so if asked and explain what you would do.")
        convo = "\n".join(f"{m['role'].upper()}: {m['text']}" for m in history[-10:])
        prompt = (f"Earlier in this chat:\n{convo}\n\n" if convo else "") + f"OWNER: {message}"
        started = time.time()
        res = await self.session(prompt, CHAT_PROMPT.format(trade_note=note), tool_names(can_trade), cycle_id=None,
                                 agent="pm", max_turns=15, channel="chat")
        log.info("chat answered in %.1fs", time.time() - started)
        return (res.get("result") or "\n".join(res["texts"]) or "(no answer)").strip()

    async def daily_report(self, context: str) -> str:
        names = ["get_portfolio", "get_trade_journal", "get_market_overview", "get_news"]
        res = await self.session(context, DAILY_PROMPT, names, cycle_id=None, agent="pm", max_turns=8,
                                 channel="quiet", web=False)
        return (res.get("result") or "\n".join(res["texts"]) or "").strip()

