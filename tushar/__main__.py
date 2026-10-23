"""Command line: `python -m tushar [run|phone|doctor|cycle|login]`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import time

from . import __version__
from .config import Settings

BANNER = r"""
  _____         _                       _
 |_   _|_ _ ___| |__   __ _ _ __   __ _(_)
   | |/ _` / __| '_ \ / _` | '__| / _` | |
   | | (_| \__ \ | | | (_| | | _ | (_| | |
   |_|\__,_|___/_| |_|\__,_|_|(_) \__,_|_|   v{v}
   Claude is the trader.
"""


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_run(s: Settings, phone: bool = False) -> int:
    problems = s.validate_startup()
    if problems:
        for p in problems:
            print(f"  x {p}")
        return 2
    import uvicorn

    from .netutil import PHONE, dashboard_urls, login_link
    from .server import create_app
    ensure_access(s, phone)
    print(BANNER.format(v=__version__))
    print(f"  Mode: {s.mode_label} | Brain: {s.brain} ({s.claude_model}) | Desk: {s.desk_mode}")
    print(f"  On this computer:  http://127.0.0.1:{s.port}")
    urls = dashboard_urls(s.host, s.port)
    if urls or phone:
        print("  Same Wi-Fi link:   " + ("   or   ".join(urls) if urls else
              f"http://<this computer's IPv4 address>:{s.port}  (find it with `ipconfig`)"))
        print(f"  Access key:        {s.dashboard_password}" if s.dashboard_password else
              "  ! No DASHBOARD_PASSWORD set: anyone on your network can open the dashboard.")
        if phone and s.phone_tunnel:
            print("  Creating a secure link that works on mobile data too (a few seconds)...")
    elif not phone:
        print("  Want it on your phone? Start with:  run.bat phone   (or ./run.sh phone)")
    print("  (On the phone never type 'localhost': that means the phone itself.)")
    print()

    def announce(url: str) -> None:
        PHONE["public_url"] = url
        link = login_link(url, s.dashboard_password)
        bar = "=" * 66
        print(f"\n  {bar}\n  OPEN ON YOUR PHONE  (Wi-Fi or mobile data, from anywhere)\n\n      {url}\n")
        print(f"  Access key: {s.dashboard_password}    Scan this QR to log in automatically:\n")
        _print_qr(link)
        print(f"  The link changes each time you start phone mode.\n  {bar}\n", flush=True)

    async def serve() -> None:
        # The default event loop on Windows is the Proactor loop, which can start the Claude Code
        # subprocess (older uvicorn versions forced a loop that cannot), so uvicorn uses loop="none".
        config = uvicorn.Config(create_app(s), host=s.host, port=s.port, log_level="warning", loop="none")
        server = uvicorn.Server(config)
        tunnel = None
        extra: list[asyncio.Task] = []
        if phone and s.phone_tunnel:
            from .tunnel import Tunnel
            tunnel = Tunnel(s.port, s.data_dir, announce, state=PHONE)
            PHONE["tunnel"] = "starting"
            extra.append(asyncio.create_task(tunnel.run()))
        if phone:
            async def open_browser() -> None:
                await asyncio.sleep(4)
                local = login_link(f"http://127.0.0.1:{s.port}", s.dashboard_password, "/?phone=1")
                try:
                    import webbrowser
                    webbrowser.open(local)
                except Exception:  # noqa: BLE001
                    pass
            extra.append(asyncio.create_task(open_browser()))
        try:
            await server.serve()
        finally:
            for t in extra:
                t.cancel()
            if tunnel:
                tunnel.stop()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0


def _print_qr(text: str) -> None:
    try:
        import qrcode
        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(text)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:  # noqa: BLE001  (no qrcode package or a console that can't draw it)
        print("  (QR not available here: open the dashboard on this computer and click the phone icon.)")


def ensure_access(s: Settings, phone: bool) -> None:
    """Phone mode listens on the network; anything reachable from outside always gets an access key."""
    from .netutil import access_key
    if phone:
        s.host = "0.0.0.0"
    if s.host not in ("127.0.0.1", "localhost") and not s.dashboard_password:
        s.dashboard_password = access_key(s.data_dir)


def claude_binary(s: Settings) -> str | None:
    path = s.claude_cli_path or shutil.which("claude")
    if not path:
        try:
            import claude_agent_sdk
            from pathlib import Path
            base = Path(claude_agent_sdk.__file__).parent / "_bundled"
            for name in ("claude.exe", "claude"):
                if (base / name).exists():
                    path = str(base / name)
                    break
        except ImportError:
            pass
    return path


def cmd_login(s: Settings) -> int:
    """Open Claude Code so you can log in with your Pro/Max account (no separate install needed)."""
    path = claude_binary(s)
    if not path:
        print("Claude Code not found. Install it from https://claude.ai/download or `npm i -g @anthropic-ai/claude-code`.")
        return 1
    print("Opening Claude Code. Log in with your Claude Pro/Max account (type /login if it doesn't ask),")
    print("then type /exit to come back. Tushar.ai will use that login.\n")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    return subprocess.call([path], env=env)


TOKEN_RE = re.compile(r"sk-ant-oat\d*-[A-Za-z0-9_\-]{20,}")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")


TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_\-]+$")


def find_token(text: str) -> str | None:
    """Pull the Claude token out of `claude setup-token` output, even if a narrow screen wrapped it."""
    clean = ANSI_RE.sub("", text).replace("\r", "\n")
    clean = re.sub(r"[\u2500-\u257f]", " ", clean)           # box-drawing borders
    lines = [ln.strip() for ln in clean.split("\n")]
    for i, ln in enumerate(lines):
        start = ln.find("sk-ant-oat")
        if start < 0:
            continue
        m = re.match(r"[A-Za-z0-9_\-]+", ln[start:])
        tok = m.group(0) if m else ""
        if start + len(tok) == len(ln):                        # token ran to the edge: it may continue
            for nxt in lines[i + 1:]:
                if not nxt or not TOKEN_CHARS.match(nxt):
                    break
                tok += nxt
        if TOKEN_RE.fullmatch(tok):
            return tok
    return None


def _run_in_wide_terminal(argv: list[str], env: dict, cols: int = 400) -> bytes:
    """Run an interactive program in a wide pseudo-terminal (so long tokens never wrap), relaying
    keyboard input and showing its output, and return everything it printed."""
    import fcntl
    import select
    import struct
    import termios
    import tty
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, cols, 0, 0))
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    os.close(slave)
    captured = bytearray()
    in_fd, out_fd = sys.stdin.fileno(), sys.stdout.fileno()
    saved = termios.tcgetattr(in_fd) if os.isatty(in_fd) else None
    if saved:
        tty.setraw(in_fd)
    try:
        while True:
            ready, _, _ = select.select([master, in_fd], [], [], 0.25)
            if master in ready:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                captured.extend(data)
                os.write(out_fd, data)
            if in_fd in ready:
                data = os.read(in_fd, 1024)
                if data:
                    os.write(master, data)
            if not ready and proc.poll() is not None:
                break
    finally:
        if saved:
            termios.tcsetattr(in_fd, termios.TCSADRAIN, saved)
        os.close(master)
        proc.wait()
    return bytes(captured)


def token_file(s: Settings):
    from pathlib import Path
    return Path(s.data_dir) / "claude_token.txt"


def load_saved_token(s: Settings) -> None:
    """Use a token saved by `token --save` when CLAUDE_CODE_OAUTH_TOKEN isn't set."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return
    f = token_file(s)
    if f.exists():
        tok = f.read_text().strip()
        if tok:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = tok


def cmd_token(s: Settings, save: bool = False) -> int:
    """Create a long-lived Claude subscription token (for a server, or to skip `login`)."""
    path = claude_binary(s)
    if not path:
        print("Claude Code not found. Run `run.bat login` first, or install Claude Code.")
        return 1
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")}
    print("Approve with your Claude Pro/Max account. On a server with no browser: open the link it shows")
    print("on your phone, approve, then paste the code back here.\n")
    if not save or sys.platform == "win32":
        print("Copy the token it prints (starts with sk-ant-). Treat it like a password.\n")
        return subprocess.call([path, "setup-token"], env=env)
    captured = _run_in_wide_terminal([path, "setup-token"], env)
    tok = find_token(captured.decode(errors="replace"))
    if not tok:
        print("\nNo token found in the output. Run it again, or paste the token into .env as CLAUDE_CODE_OAUTH_TOKEN.")
        return 1
    f = token_file(s)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(tok)
    try:
        f.chmod(0o600)
    except OSError:
        pass
    print(f"\nSaved. Tushar.ai will use this Claude login from now on ({f}).")
    return 0


async def _doctor(s: Settings) -> int:
    ok = True

    def line(good: bool, text: str, optional: bool = False) -> None:
        nonlocal ok
        if not optional:
            ok = ok and good
        print(f"  {'OK ' if good else ('!! ' if optional else 'XX ')} {text}")

    print(BANNER.format(v=__version__))
    print(f"Mode: {s.mode_label}   Brain: {s.brain}   Model: {s.claude_model}\n")
    for p in s.validate_startup():
        line(False, p)

    # --- market data / account ---
    from .engine import Engine
    try:
        eng = Engine(s, db_path=":memory:")
    except Exception as e:  # noqa: BLE001
        line(False, f"engine setup: {e}")
        return 1
    try:
        await eng.market.refresh_symbols()
        await eng.market.refresh_prices()
        btc = eng.market.price("BTCUSDT")
        line(bool(btc), f"market data: {len(eng.market.symbols)} symbols, BTCUSDT mark {btc}")
    except Exception as e:  # noqa: BLE001
        line(False, f"market data: {e}")
    if s.uses_exchange_account:
        try:
            await eng.broker.prepare()
            acct = await eng.broker.account()
            line(True, f"Binance {s.trading_mode} account: equity {acct.equity:,.2f} USDT, "
                       f"available {acct.available:,.2f}, {len(acct.positions)} open position(s)"
                       + (" (hedge mode)" if eng.broker.hedge_mode else " (one-way mode)"))
            orders = await eng.broker.open_orders()
            line(True, f"open orders readable ({len(orders)}); algo order API "
                       f"{'available' if eng.broker.algo_supported else 'missing (classic fallback)'}")
        except Exception as e:  # noqa: BLE001
            line(False, f"Binance account: {e}")
    else:
        acct = await eng.broker.account()
        line(True, f"paper account: equity {acct.equity:,.2f} USDT")

    # --- news ---
    try:
        n = await eng.news.refresh()
        good = [k for k, v in eng.news.status.items() if v.startswith("ok")]
        line(bool(good), f"news: {len(good)}/{len(eng.news.status)} feeds reachable, {n} headlines"
                         + ("" if good else " (optional: Claude can still use web search)"), optional=True)
    except Exception as e:  # noqa: BLE001
        line(False, f"news: {e}", optional=True)

    # --- Claude ---
    if s.brain == "mock":
        line(True, "brain: mock (no Claude calls)")
    else:
        try:
            from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, SystemMessage, TextBlock, query
            opts = ClaudeAgentOptions(tools=[], max_turns=1, model=s.claude_model or None, setting_sources=[],
                                      cli_path=s.claude_cli_path or None, permission_mode="dontAsk")
            started = time.time()
            auth, text, err = None, "", None
            async for msg in query(prompt="Reply with exactly: TUSHAR OK", options=opts):
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    auth = msg.data.get("apiKeySource")
                elif isinstance(msg, AssistantMessage):
                    text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                    if getattr(msg, "error", None):
                        err = msg.error
                elif isinstance(msg, ResultMessage) and msg.is_error:
                    err = msg.result or msg.subtype
            good = "TUSHAR OK" in text and not err
            line(good, f"Claude answered in {time.time() - started:.1f}s: {text.strip()[:60]!r}"
                       + (f" error={err}" if err else ""))
            if auth in (None, "none", "/login managed key"):
                line(True, f"Claude auth: subscription login (apiKeySource={auth})")
            else:
                line(False, f"Claude auth source is {auth}: that bills an API key. Unset it to use your subscription.")
        except Exception as e:  # noqa: BLE001
            line(False, f"Claude: {e}. Run `claude` once and log in with your Pro/Max account, "
                        "or set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`).")
    await eng.source.close()
    print("\nAll good. Start with: python -m tushar" if ok else "\nFix the XX lines above, then run again.")
    return 0 if ok else 1


async def _cycle(s: Settings) -> int:
    from .engine import Engine
    eng = Engine(s)
    await eng.start(loops=False)

    q = eng.bus.subscribe()

    async def printer():
        while True:
            m = await q.get()
            d = m.get("data") or {}
            if m["type"] in ("thought", "thinking"):
                print(f"\n[{m['type']}] {d.get('text', '')}")
            elif m["type"] == "tool_call":
                print(f"  -> {d.get('name')} {d.get('input')}")
            elif m["type"] == "tool_result":
                print(f"  <- {str(d.get('text', ''))[:300]}")
            elif m["type"] in ("trade_open", "trade_close", "notice"):
                print(f"  ** {m['type']}: {d}")

    t = asyncio.create_task(printer())
    res = await eng.run_cycle("manual (command line)")
    await asyncio.sleep(0.2)
    t.cancel()
    print(f"\nCycle #{res['cycle_id']} {res['status']}" + (f": {res['error']}" if res.get("error") else ""))
    await eng.stop()
    return 0 if res["status"] == "done" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tushar", description="Tushar.ai - Claude trades crypto futures")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "phone", "doctor", "cycle", "login", "token"],
                    help="run = bot + dashboard (default); phone = same, reachable from your phone with a PIN; "
                         "doctor = check the setup; cycle = one Claude cycle in the terminal; "
                         "login = log in to Claude; token = Claude token for a server")
    ap.add_argument("--env", help="path to a .env file")
    ap.add_argument("--log", default=os.environ.get("LOG_LEVEL", "INFO"))
    ap.add_argument("--save", action="store_true", help="token: save it into the data folder for Tushar.ai to use")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: never crash on an emoji
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    setup_logging(args.log)
    try:
        s = Settings.from_env(args.env)
    except ValueError as e:
        print(f"Config error: {e}")
        return 2
    if args.command == "login":
        return cmd_login(s)
    if args.command == "token":
        return cmd_token(s, save=args.save)
    load_saved_token(s)
    if args.command == "doctor":
        return asyncio.run(_doctor(s))
    if args.command == "cycle":
        problems = s.validate_startup()
        if problems:
            print("\n".join(problems))
            return 2
        return asyncio.run(_cycle(s))
    return cmd_run(s, phone=args.command == "phone")


if __name__ == "__main__":
    sys.exit(main())
