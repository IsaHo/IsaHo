"""Distinct client IPs per user, read from Xray's access log (real IPs need PROXY protocol
from the relays, see xray.split_relay_inbound)."""
import os
import re
import time

from xray import ACCESS_LOG

WINDOW = 180            # an IP counts as a device for this long after its last connection
MAX_LOG = 20 * 1024 ** 2
_LINE = re.compile(r"from (?:tcp:|udp:)?\[?([0-9a-fA-F.:]+?)\]?:\d+ accepted .*?email: (\S+)")
_pos = 0
seen = {}               # email -> {ip: last_seen}


def scan() -> None:
    global _pos
    try:
        size = os.path.getsize(ACCESS_LOG)
    except OSError:
        return
    if size < _pos:
        _pos = 0
    with open(ACCESS_LOG, errors="replace") as f:
        f.seek(_pos)
        data = f.read()
        _pos = f.tell()
    now = time.time()
    import links
    relay_ips = {h for h, _ in links.relays()}
    for ip, email in _LINE.findall(data):
        if not ip.startswith("127.") and ip not in relay_ips:
            seen.setdefault(email, {})[ip] = now
    for email in list(seen):
        seen[email] = {ip: t for ip, t in seen[email].items() if now - t < WINDOW}
        if not seen[email]:
            del seen[email]
    if _pos > MAX_LOG:  # Xray appends, so truncating in place is safe
        open(ACCESS_LOG, "w").close()
        _pos = 0


def network(ip: str) -> str:
    """Group addresses that are probably the same device. Iranian mobile carriers hand out a
    different public IPv4 from a large pool for each connection, so a single phone shows up as
    many IPs; they stay inside the same /16. IPv6 devices keep one /64."""
    if ":" in ip:
        return ":".join(ip.split(":")[:4]) + "::/64"
    return ".".join(ip.split(".")[:2]) + ".0.0/16"


def networks(email: str) -> list:
    return sorted({network(ip) for ip in seen.get(email, {})})


def count(email: str) -> int:
    return len(networks(email))
