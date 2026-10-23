"""In-process pub/sub that feeds the dashboard's WebSocket."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

# Events kept in memory so a freshly opened dashboard can replay Claude's recent thinking.
MIND_TYPES = {"cycle_start", "thought", "thinking", "tool_call", "tool_result", "cycle_end",
              "trade_open", "trade_close", "alert_hit", "error", "notice"}


class EventBus:
    def __init__(self, mind_size: int = 400):
        self._subs: set[asyncio.Queue] = set()
        self.mind: deque[dict] = deque(maxlen=mind_size)
        self.log: deque[dict] = deque(maxlen=200)

    def publish(self, type_: str, data: Any = None) -> dict:
        msg = {"type": type_, "ts": time.time(), "data": data}
        if type_ in MIND_TYPES:
            self.mind.append(msg)
        if type_ == "log":
            self.log.append(msg)
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # A slow client loses messages instead of slowing the bot down.
                pass
        return msg

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
