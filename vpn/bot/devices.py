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
    for ip, email in _LINE.findall(data):
        if not ip.startswith("127."):
            seen.setdefault(email, {})[ip] = now
    for email in list(seen):
        seen[email] = {ip: t for ip, t in seen[email].items() if now - t < WINDOW}
        if not seen[email]:
            del seen[email]
    if _pos > MAX_LOG:  # Xray appends, so truncating in place is safe
        open(ACCESS_LOG, "w").close()
        _pos = 0


def count(email: str) -> int:
    return len(seen.get(email, {}))
