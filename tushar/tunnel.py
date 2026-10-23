"""Secure public link for your phone, via a free Cloudflare quick tunnel.

`run.bat phone` starts `cloudflared tunnel --url http://127.0.0.1:PORT`, which
gives an https://<random>.trycloudflare.com address that reaches the dashboard
from anywhere (Wi-Fi or mobile data), with no router or firewall setup: the
connection goes out from your PC, nothing comes in. The dashboard stays behind
its access key. cloudflared is downloaded once into data/bin if it isn't
installed.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger("tushar.tunnel")

# The public address is random hyphenated words; never the API host cloudflared also logs.
URL_RE = re.compile(r"https://(?!api\.)[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"


def asset_name() -> tuple[str, bool]:
    """Release file for this machine and whether it is a .tgz."""
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        return ("cloudflared-windows-arm64.exe" if arm else "cloudflared-windows-amd64.exe"), False
    if sys.platform == "darwin":
        return ("cloudflared-darwin-arm64.tgz" if arm else "cloudflared-darwin-amd64.tgz"), True
    return ("cloudflared-linux-arm64" if arm else "cloudflared-linux-amd64"), False


def parse_url(line: str) -> str | None:
    m = URL_RE.search(line)
    return m.group(0) if m else None


async def ensure_cloudflared(data_dir: str, say: Callable[[str], None] = print) -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    exe = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    target = Path(data_dir) / "bin" / exe
    if target.exists() and target.stat().st_size > 1_000_000:
        return str(target)
    name, is_tgz = asset_name()
    say("  Downloading cloudflared (one time, ~55 MB) for the phone link...")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=120)) as client:
            async with client.stream("GET", RELEASES + name) as resp:
                resp.raise_for_status()
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
    except Exception as e:  # noqa: BLE001
        say(f"  ! Could not download cloudflared ({type(e).__name__}). Phone link over the internet is off;")
        say("    the Wi-Fi link still works. Or install it yourself: https://github.com/cloudflare/cloudflared/releases")
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_tgz:
        with tarfile.open(fileobj=io.BytesIO(bytes(data))) as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("cloudflared"))
            target.write_bytes(tar.extractfile(member).read())  # type: ignore[union-attr]
    else:
        target.write_bytes(bytes(data))
    if sys.platform != "win32":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(target)


class Tunnel:
    def __init__(self, port: int, data_dir: str, on_url: Callable[[str], None], binary: str | None = None,
                 say: Callable[[str], None] = print, state: dict | None = None):
        self.port = port
        self.data_dir = data_dir
        self.on_url = on_url
        self.binary = binary
        self.say = say
        self.url: str | None = None
        self.state = state if state is not None else {}
        self.status = "starting"
        self.last_error = ""
        self._warned = False
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = value
        if hasattr(self, "state"):
            self.state["tunnel"] = value

    async def run(self) -> None:
        binary = self.binary or await ensure_cloudflared(self.data_dir, self.say)
        if not binary:
            self.status = "unavailable"
            return
        delay = 3
        try:
            while True:
                self.status = "connecting"
                self._proc = await asyncio.create_subprocess_exec(
                    binary, "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env={**os.environ, "NO_COLOR": "1"})
                assert self._proc.stdout is not None
                async for raw in self._proc.stdout:
                    line = raw.decode(errors="replace")
                    url = parse_url(line)
                    if url and url != self.url:
                        self.url = url
                        self.status = "live"
                        self.on_url(url)
                    if "failed" in line.lower() or " ERR " in line:
                        self.last_error = line.strip()[-160:]
                    log.debug("cloudflared: %s", line.rstrip())
                await self._proc.wait()
                if not self.url and not self._warned:
                    self._warned = True
                    self.say(f"  ! Couldn't create the internet phone link yet ({self.last_error or 'cloudflared exited'}).")
                    self.say("    Retrying in the background. Meanwhile use the Same Wi-Fi link above.")
                self.status = "failed" if not self.url else "reconnecting"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        finally:
            self.stop()

    def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
