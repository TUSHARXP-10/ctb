"""Brains: who makes the trading decisions."""


def make_brain(kind: str, engine):
    """agent = Claude Agent SDK with live tools (default), cli = `claude -p` single pass, mock = rules."""
    if kind == "agent":
        from .agent import AgentBrain
        return AgentBrain(engine)
    if kind == "cli":
        from .cli import CliBrain
        return CliBrain(engine)
    from .mock import MockBrain
    return MockBrain(engine)
