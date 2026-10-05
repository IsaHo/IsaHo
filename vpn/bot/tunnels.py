"""Health of SSH tunnels from Iranian relays, seen from this (foreign) server's side."""
import socket

import psutil

import links
from config import cfg


def _resolve(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def status() -> list:
    """[(label, host, port, sessions)] where sessions = established SSH connections from that relay."""
    conns = [c for c in psutil.net_connections(kind="tcp")
             if c.status == psutil.CONN_ESTABLISHED and c.laddr and c.laddr.port == cfg.ssh_port and c.raddr]
    import relays
    out = []
    for i, (host, port) in enumerate(links.relays(), 1):
        ips = {_resolve(host)}
        egress = (relays.reports.get(host) or {}).get("egress")
        if egress:
            ips.add(egress)  # providers that NAT outgoing traffic connect from another IP
        out.append((f"IR{i}", host, port, sum(1 for c in conns if c.raddr.ip in ips)))
    return out


def summary() -> str:
    rows = status()
    if not rows:
        return "سرور واسط تعریف نشده"
    return "\n".join(f"{'🟢' if n else '🔴'} {label} <code>{host}</code> — {n} تانل SSH" for label, host, _, n in rows)
