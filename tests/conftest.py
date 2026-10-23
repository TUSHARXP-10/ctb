import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Never let a developer's .env leak into the tests.
for key in list(os.environ):
    if key.startswith(("BINANCE_", "TRADING_MODE", "MARKET_SOURCE", "BRAIN", "DASHBOARD_PASSWORD", "CLAUDE_MODEL")):
        os.environ.pop(key)

from tushar.config import Settings  # noqa: E402

# A fixed clock so the simulated market is identical on every run.
SIM_NOW_MS = 1_792_000_000_000


def make_settings(tmp_path, **over) -> Settings:
    s = Settings()
    s.trading_mode = "paper"
    s.market_source = "sim"
    s.brain = "mock"
    s.data_dir = str(tmp_path)
    s.tm_replays = 2000
    s.min_reward_risk = 1.5
    for k, v in over.items():
        setattr(s, k, v)
    s._clamp_all()
    return s


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
async def engine(tmp_path):
    from tushar.engine import Engine
    eng = Engine(make_settings(tmp_path), db_path=tmp_path / "t.db", sim_now_ms=SIM_NOW_MS)
    await eng.start(loops=False)
    yield eng
    await eng.stop()
