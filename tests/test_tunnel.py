import asyncio
import sys

import pytest

from tushar.netutil import access_key, login_link
from tushar.tunnel import Tunnel, parse_url


def test_parse_url_ignores_api_host():
    assert parse_url('failed: Post "https://api.trycloudflare.com/tunnel"') is None
    assert parse_url("|  https://brave-tiger-lunar-demo.trycloudflare.com  |") == \
        "https://brave-tiger-lunar-demo.trycloudflare.com"


def test_access_key_is_strong_and_stable(tmp_path):
    k = access_key(str(tmp_path))
    assert len(k) == 14 and k.count("-") == 2 and not set(k) & set("0o1li")
    assert access_key(str(tmp_path)) == k
    assert login_link("https://x-y.trycloudflare.com", k, "/?phone=1").startswith(
        f"https://x-y.trycloudflare.com/login#k={k}&next=/%3Fphone%3D1")


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as a fake cloudflared")
async def test_tunnel_reports_url_and_failures(tmp_path):
    good = tmp_path / "good.sh"
    good.write_text('#!/bin/sh\necho "INF |  https://alpha-beta-gamma.trycloudflare.com  |"\nsleep 30\n')
    bad = tmp_path / "bad.sh"
    bad.write_text('#!/bin/sh\necho \'failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel"\'\n')
    for f in (good, bad):
        f.chmod(0o755)
    urls, said, state = [], [], {}
    t = Tunnel(1, str(tmp_path), urls.append, binary=str(good), say=said.append, state=state)
    task = asyncio.create_task(t.run())
    for _ in range(50):
        await asyncio.sleep(0.05)
        if urls:
            break
    assert urls == ["https://alpha-beta-gamma.trycloudflare.com"] and state["tunnel"] == "live"
    task.cancel()
    t.stop()
    t2 = Tunnel(1, str(tmp_path), urls.append, binary=str(bad), say=said.append, state=state)
    task2 = asyncio.create_task(t2.run())
    for _ in range(50):
        await asyncio.sleep(0.05)
        if said:
            break
    task2.cancel()
    t2.stop()
    assert state["tunnel"] == "failed" and any("Couldn't create" in s for s in said)


def test_network_exposure_always_gets_a_key(tmp_path):
    from tushar.__main__ import ensure_access
    from .conftest import make_settings
    s = make_settings(tmp_path)
    ensure_access(s, phone=False)
    assert s.host == "127.0.0.1" and s.dashboard_password == ""      # local only: no key needed
    s.host = "0.0.0.0"
    ensure_access(s, phone=False)
    assert len(s.dashboard_password) == 14                           # reachable: key created
    s2 = make_settings(tmp_path, dashboard_password="mine")
    ensure_access(s2, phone=True)
    assert s2.host == "0.0.0.0" and s2.dashboard_password == "mine"  # your own password wins
