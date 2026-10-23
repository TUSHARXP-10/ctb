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
