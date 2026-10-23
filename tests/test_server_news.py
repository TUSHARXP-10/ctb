from fastapi.testclient import TestClient

from tushar.engine import Engine
from tushar.news import analyze_headline, parse_feed
from tushar.server import create_app

from .conftest import SIM_NOW_MS, make_settings

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Bitcoin ETF inflows surge as SEC approves new fund</title><link>https://ex.com/a</link>
<description>&lt;p&gt;Spot bitcoin ETFs saw record inflows.&lt;/p&gt;</description>
<pubDate>Wed, 21 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>DeFi protocol drained in $40M exploit</title><link>https://ex.com/b</link></item>
</channel></rss>"""


def test_headline_analysis():
    a = analyze_headline("Solana plunges after exchange hack, liquidations top $500M")
    assert "hack" in a["tags"] and "liquidations" in a["tags"]
    assert "SOLUSDT" in a["symbols"] and a["sentiment"] < 0
    b = analyze_headline("Fed signals rate cut; bitcoin rallies to record high")
    assert "macro" in b["tags"] and "BTCUSDT" in b["symbols"] and b["sentiment"] > 0


def test_parse_feed():
    items = parse_feed("Test", RSS)
    assert len(items) == 2
    assert items[0]["summary"] == "Spot bitcoin ETFs saw record inflows."
    assert "etf" in items[0]["tags"] and "BTCUSDT" in items[0]["symbols"]
    assert "hack" in items[1]["tags"]


def _app(tmp_path, **over):
    s = make_settings(tmp_path, **over)
    eng = Engine(s, db_path=tmp_path / "s.db", sim_now_ms=SIM_NOW_MS)
    return create_app(s, engine=eng, start_loops=False)


def test_api_state_and_controls(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        st = c.get("/api/state").json()
        assert st["status"]["mode"] == "SIMULATED MARKET"
        assert st["settings"]["binance_api_secret"] is False
        assert "BTCUSDT" in st["tickers"]
        k = c.get("/api/klines", params={"symbol": "BTCUSDT", "interval": "1h", "limit": 100}).json()
        assert len(k["candles"]) == 100
        assert c.post("/api/control/pause").json()["status"]["state"] == "paused"
        assert c.post("/api/control/resume").json()["status"]["state"] == "active"
        r = c.post("/api/settings", json={"cycle_minutes": 1, "max_open_positions": 3}).json()
        assert r["settings"]["cycle_minutes"] == 3 and r["settings"]["max_open_positions"] == 3
        assert c.get("/").status_code == 200
        tm = c.post("/api/timemachine", json={"symbol": "BTCUSDT", "side": "long", "stop_loss": 1,
                                              "take_profit": 10_000_000}).json()
        assert tm["ok"]


def test_password_protects_api(tmp_path):
    with TestClient(_app(tmp_path, dashboard_password="s3cret")) as c:
        assert c.get("/api/state").status_code == 401
        assert c.get("/", follow_redirects=False).status_code in (302, 307)
        assert c.post("/api/login", json={"password": "nope"}).status_code == 401
        assert c.post("/api/login", json={"password": "s3cret"}).status_code == 200
        assert c.get("/api/state").status_code == 200
