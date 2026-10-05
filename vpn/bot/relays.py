"""State reported by the agents on Iranian relays, and actions queued for them.

Agents POST to /relay/report on the local-only subscription port, which they reach through
their own SSH tunnels; the reply carries any queued action (restart / update)."""
import subprocess
import time

reports = {}   # relay ip -> latest report (+ "seen" timestamp and computed rates)
pending = {}   # relay ip -> {"action": ..., "ref": ...}

STALE = 180    # seconds without a report before a relay is shown as offline


def record(data: dict) -> dict:
    ip = str(data.get("ip") or data.get("host") or "?")[:64]
    now = time.time()
    prev = reports.get(ip)
    if prev and now > prev["seen"]:
        dt = now - prev["seen"]
        data["rx_rate"] = max(0, (data.get("rx", 0) - prev.get("rx", 0)) / dt)
        data["tx_rate"] = max(0, (data.get("tx", 0) - prev.get("tx", 0)) / dt)
    data["seen"] = now
    reports[ip] = data
    import xray
    return {**pending.pop(ip, {}), "proxy": xray.split_relay_inbound()}


def queue(ip: str, action: str, ref: str = "") -> None:
    pending[ip] = {"action": action, "ref": ref}


def current_ref() -> str:
    """Commit of this server's checkout, so relays update to the exact same version."""
    try:
        repo = open("/opt/isaho-vpn/repo_path").read().strip()
        out = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        ref = out.stdout.strip()
        if len(ref) == 40:
            return ref
    except (OSError, subprocess.SubprocessError):
        pass
    return "main"


def is_local_request(request) -> bool:
    sock = request.transport.get_extra_info("sockname") if request.transport else None
    peer = request.remote or ""
    return bool(sock) and sock[0] == "127.0.0.1" and peer.startswith("127.")


def ready_for_real_ip() -> tuple:
    """(ok, reason): every configured relay must run an agent that can switch PROXY protocol."""
    import links
    for host, _ in links.relays():
        r = reports.get(host)
        if not r or not online(host):
            return False, f"سرور {host} گزارش نمی‌دهد"
        if int(r.get("agent", 1)) < 2:
            return False, f"سرور {host} قدیمی است؛ اول «⬆️ آپدیت همه» را بزنید"
    return True, ""


def online(ip: str) -> bool:
    r = reports.get(ip)
    return bool(r) and time.time() - r["seen"] < STALE

