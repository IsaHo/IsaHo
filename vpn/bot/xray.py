"""Xray config generation, hot user add/remove via the Xray API, and traffic stats."""
import asyncio
import json
import logging
import os
import tempfile
import time

import db
from config import cfg

log = logging.getLogger(__name__)

REALITY_TAG = "reality"
RELAY_TAG = "reality-relay"
ACCESS_LOG = "/var/log/xray/access.log"
CDN_TAG = "cdn"


def split_relay_inbound() -> bool:
    """Serve relays on 127.0.0.1:443 with PROXY protocol (real client IPs) and the public on the
    server IP. Turned on from the bot once relays are ready (they switch HAProxy to send the
    PROXY header in step), and only possible when the public IP sits on a local interface."""
    if db.get_setting("real_ip") != "1":
        return False
    try:
        import psutil
        return any(a.address == cfg.server_ip for addrs in psutil.net_if_addrs().values() for a in addrs)
    except Exception:
        return False


def all_tags() -> tuple:
    return (REALITY_TAG, RELAY_TAG, CDN_TAG) if split_relay_inbound() else (REALITY_TAG, CDN_TAG)
_lock = asyncio.Lock()
last_rate = 0.0  # users' bytes/s over the last stats interval
_last_flush = 0.0


def _clients(users, flow: str = "") -> list:
    out = []
    for u in users:
        c = {"id": u.uuid, "email": u.name, "level": 0}
        if flow:
            c["flow"] = flow
        out.append(c)
    return out


def _inbounds(users) -> list:
    sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}
    inbounds = [
        {
            "tag": REALITY_TAG,
            "listen": "0.0.0.0",
            "port": cfg.reality_port,
            "protocol": "vless",
            "settings": {"clients": _clients(users, "xtls-rprx-vision"), "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": cfg.reality_dest,
                    "xver": 0,
                    "serverNames": [cfg.reality_sni],
                    "privateKey": cfg.reality_private_key,
                    "shortIds": [cfg.reality_short_id],
                },
                "sockopt": {"tcpFastOpen": True},
            },
            "sniffing": sniffing,
        },
        {
            "tag": CDN_TAG,
            "listen": "0.0.0.0",
            "port": cfg.cdn_port,
            "protocol": "vless",
            "settings": {"clients": _clients(users), "decryption": "none"},
            "streamSettings": {
                "network": "xhttp",
                "security": "tls",
                "tlsSettings": {
                    "alpn": ["h2", "http/1.1"],
                    "certificates": [{"certificateFile": cfg.cert_file, "keyFile": cfg.key_file}],
                },
                "xhttpSettings": {"path": cfg.cdn_path, "mode": "auto"},
                "sockopt": {"tcpFastOpen": True},
            },
            "sniffing": sniffing,
        },
    ]
    if split_relay_inbound():
        reality = inbounds[0]
        reality["listen"] = cfg.server_ip
        relay = json.loads(json.dumps(reality))
        relay["tag"], relay["listen"] = RELAY_TAG, "127.0.0.1"
        relay["streamSettings"]["sockopt"]["acceptProxyProtocol"] = True
        inbounds.append(relay)
    return inbounds


def build_config(users) -> dict:
    api_host, api_port = cfg.api_addr.rsplit(":", 1)
    return {
        # the access log is only useful (and only kept) when relays pass real client IPs
        "log": {"loglevel": "warning", "access": ACCESS_LOG if split_relay_inbound() else "none"},
        "api": {"tag": "api", "services": ["HandlerService", "StatsService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True, "statsUserOnline": True,
                             "handshake": 4, "connIdle": 300, "bufferSize": 64}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
        },
        "inbounds": [
            {"tag": "api", "listen": api_host, "port": int(api_port),
             "protocol": "dokodemo-door", "settings": {"address": api_host}},
            *_inbounds(users),
        ],
        "outbounds": [
            {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIPv4"}},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
                {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
                # Iranian sites should never be reached from the server: it keeps
                # the server IP out of their logs (a common way servers get flagged).
                {"type": "field", "domain": ["regexp:\\.ir$"], "outboundTag": "block"},
                {"type": "field", "ip": ["geoip:ir"], "outboundTag": "block"},
            ],
        },
    }


async def _run(*args, timeout: int = 15) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "", "timeout"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def write_config() -> None:
    """Write the full config for all enabled users, validating it before replacing."""
    conf = build_config(db.active_users())
    os.makedirs(os.path.dirname(ACCESS_LOG), exist_ok=True)
    path = cfg.xray_config
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(conf, f, indent=2)
    code, out, err = await _run(cfg.xray_bin, "run", "-test", "-c", tmp)
    if code != 0:
        os.unlink(tmp)
        raise RuntimeError(f"invalid xray config: {out}{err}"[-1500:])
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


async def collect_stats() -> dict:
    """Read and reset per-user counters. Returns {name: (up, down)}."""
    code, out, err = await _run(cfg.xray_bin, "api", "statsquery",
                                f"--server={cfg.api_addr}", "-pattern", "user>>>", "-reset")
    if code != 0:
        log.debug("statsquery failed: %s", err)
        return {}
    try:
        stats = json.loads(out or "{}").get("stat", []) or []
    except json.JSONDecodeError:
        return {}
    result = {}
    for s in stats:
        parts = s.get("name", "").split(">>>")  # user>>>NAME>>>traffic>>>uplink
        if len(parts) != 4:
            continue
        up, down = result.get(parts[1], (0, 0))
        value = int(s.get("value", 0) or 0)
        if parts[3] == "uplink":
            up += value
        else:
            down += value
        result[parts[1]] = (up, down)
    return result


async def flush_stats() -> None:
    global last_rate, _last_flush
    stats = await collect_stats()
    now = time.monotonic()
    total = sum(up + down for up, down in stats.values())
    if _last_flush and now > _last_flush:
        last_rate = total / (now - _last_flush)
    _last_flush = now
    if stats:
        db.add_traffic(stats)


async def restart() -> bool:
    await flush_stats()  # counters live in memory and are lost on restart
    code, _, err = await _run("systemctl", "restart", "xray")
    if code != 0:
        log.error("xray restart failed: %s", err)
    return code == 0


async def is_active() -> bool:
    code, out, _ = await _run("systemctl", "is-active", "xray")
    return out.strip() == "active"


async def _api_add(user) -> bool:
    payload = {"inbounds": [
        {"tag": ib["tag"], "port": ib["port"], "protocol": "vless", "settings": ib["settings"]}
        for ib in _inbounds([user])
    ]}
    fd, tmp = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    try:
        code, out, err = await _run(cfg.xray_bin, "api", "adu", f"--server={cfg.api_addr}", tmp)
    finally:
        os.unlink(tmp)
    return code == 0 and "error" not in (out + err).lower()


async def _api_remove(name: str) -> bool:
    ok = True
    for tag in all_tags():
        code, out, err = await _run(cfg.xray_bin, "api", "rmu", f"--server={cfg.api_addr}",
                                    f"-tag={tag}", name)
        ok = ok and code == 0 and "error" not in (out + err).lower()
    return ok


async def sync_user(user, enabled: bool) -> None:
    """Persist config, then apply to the running Xray without dropping other users.
    Falls back to a full restart when the API call fails."""
    async with _lock:
        await write_config()
        if enabled:
            await _api_remove(user.name)  # no-op if absent; avoids duplicate errors
            ok = await _api_add(user)
        else:
            ok = await _api_remove(user.name)
        if not ok:
            log.warning("xray API failed for %s, restarting xray", user.name)
            await restart()


async def apply_all() -> None:
    async with _lock:
        await write_config()
        await restart()
