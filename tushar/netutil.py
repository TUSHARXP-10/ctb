"""Find the addresses a phone on the same Wi-Fi can use to reach the dashboard."""
from __future__ import annotations

import socket


def lan_ips() -> list[str]:
    """Private IPv4 addresses of this machine, most likely Wi-Fi/LAN address first."""
    ips: list[str] = []
    try:
        # No packet is sent: connecting a UDP socket only picks the outgoing interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    private = ("192.168.", "10.", "172.", "100.")   # home/office LANs, plus Tailscale (100.x)
    out = [ip for ip in dict.fromkeys(ips) if ip.startswith(private)]
    if not out:  # unusual network: fall back to the outgoing interface itself
        out = [ip for ip in dict.fromkeys(ips) if not ip.startswith(("127.", "169.254.", "0."))][:1]
    return out


def dashboard_urls(host: str, port: int) -> list[str]:
    if host in ("0.0.0.0", "::", ""):
        return [f"http://{ip}:{port}" for ip in lan_ips()]
    if host in ("127.0.0.1", "localhost"):
        return []
    return [f"http://{host}:{port}"]


# Shared with the web server: the public phone link once the tunnel is up.
PHONE: dict = {"public_url": None, "tunnel": "off"}

_KEY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o/1/l/i: easy to read and type


def access_key(data_dir: str) -> str:
    """Strong dashboard key (xxxx-xxxx-xxxx), kept across restarts so the phone stays logged in."""
    import secrets
    from pathlib import Path
    path = Path(data_dir) / "access_key.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        key = path.read_text().strip()
        if len(key) >= 12:
            return key
    key = "-".join("".join(secrets.choice(_KEY_ALPHABET) for _ in range(4)) for _ in range(3))
    path.write_text(key)
    return key


def login_link(base_url: str, key: str, next_path: str = "/") -> str:
    """A link that logs in automatically (the key travels in the #fragment, never in server logs)."""
    from urllib.parse import quote
    return f"{base_url.rstrip('/')}/login#k={quote(key)}&next={quote(next_path)}"
