"""Xray config generation, hot user add/remove via the Xray API, and traffic stats."""
import asyncio
import json
import logging
import os
import tempfile

import db
from config import cfg

log = logging.getLogger(__name__)

REALITY_TAG = "reality"
CDN_TAG = "cdn"
_lock = asyncio.Lock()


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
    return [
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


def build_config(users) -> dict:
    api_host, api_port = cfg.api_addr.rsplit(":", 1)
    return {
        "log": {"loglevel": "warning", "access": "none"},
        "api": {"tag": "api", "services": ["HandlerService", "StatsService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True,
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
    stats = await collect_stats()
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
    for tag in (REALITY_TAG, CDN_TAG):
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
