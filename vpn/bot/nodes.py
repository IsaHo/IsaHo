"""Secondary foreign servers ("nodes"). They run Xray only, pull their config (same users and
Reality keys as this server) from the bot every minute and push traffic counters back, so a
node can take over transparently when this server is down."""
import hashlib
import json
import secrets
import ssl
import time

import db
import xray
from config import cfg

reports = {}  # name -> {"seen": ts, ...}


def all_nodes() -> list:
    try:
        return json.loads(db.get_setting("nodes") or "[]")
    except json.JSONDecodeError:
        return []


def save(nodes: list) -> None:
    db.set_setting("nodes", json.dumps(nodes))


def add(name: str, ip: str, domain: str) -> dict:
    nodes = [n for n in all_nodes() if n["name"] != name]
    # private: Xray listens on localhost only, reachable just through the relays' SSH tunnels
    node = {"name": name, "ip": ip, "domain": domain, "key": secrets.token_hex(16), "private": True}
    nodes.append(node)
    save(nodes)
    return node


def remove(name: str) -> None:
    save([n for n in all_nodes() if n["name"] != name])


def by_key(key: str):
    return next((n for n in all_nodes() if key and secrets.compare_digest(n["key"], key)), None)


def config_for(node: dict) -> dict:
    conf = xray.build_config(db.active_users(), node=True)
    if node.get("private", True):
        for inbound in conf["inbounds"]:
            inbound["listen"] = "127.0.0.1"
    return conf


def toggle_private(name: str, expected_private: bool | None = None) -> bool:
    nodes = all_nodes()
    for n in nodes:
        if n["name"] == name:
            if expected_private is not None and n.get("private", True) != expected_private:
                return False
            n["private"] = not n.get("private", True)
            n["mode_pending"] = True
            save(nodes)
            return True
    return False


def public_nodes() -> list:
    return [n for n in all_nodes() if public_ready(n)]


def public_ready(node: dict) -> bool:
    """Do not advertise a newly requested/rejected public mode before node acknowledgement."""
    return not (node.get("private", True) or node.get("mode_pending") or node.get("config_rejected"))


def cdn_domains(node: dict) -> list:
    """Published CDN hostnames for this node. Comma lets us front one origin behind several zones."""
    return [d.strip() for d in (node.get("domain") or "").split(",") if d.strip()]


def reality_proxy_port(node: dict) -> int:
    """Public port that forwards into this node's loopback Reality. 0 means not published."""
    try:
        port = int(node.get("reality_proxy_port") or 0)
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 else 0


DEFAULT_REALITY_PROXY_PORT = 8443


def set_reality_proxy_port(name: str, port: int) -> bool:
    """Enable/disable the Reality bridge publication for a node. 0 disables."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if port < 0 or port > 65535:
        return False
    stored = all_nodes()
    for n in stored:
        if n["name"] == name:
            if port:
                n["reality_proxy_port"] = port
            else:
                n.pop("reality_proxy_port", None)
            save(stored)
            return True
    return False


def has_cdn(node: dict) -> bool:
    """CDN publication is independent of a node's private Reality listener."""
    enabled = node.get("cdn_enabled", not node.get("private", True))
    return enabled is True and bool(cdn_domains(node))


def cdn_nodes() -> list:
    return [node for node in all_nodes() if has_cdn(node)]


def record_stats(node: dict, stats: dict, info: dict) -> None:
    clean = {}
    for name, pair in (stats or {}).items():
        try:
            up, down = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if up >= 0 and down >= 0 and db.get_by_name(str(name)):
            clean[str(name)] = (up, down)
    if clean:
        db.add_traffic(clean)
    prev = reports.get(node["name"], {})
    reports[node["name"]] = {**info, "seen": time.time(),
                             "bytes": prev.get("bytes", 0) + sum(u + d for u, d in clean.values())}
    apply = info.get("config_apply")
    if isinstance(apply, dict):
        state = apply.get("state")
        stored = all_nodes()
        for current in stored:
            if current["name"] != node["name"]:
                continue
            if state not in {"rejected", "restart_failed", "applied", "in_sync"}:
                break
            rejected = state in {"rejected", "restart_failed"}
            changed = current.get("config_rejected", False) != rejected
            if changed:
                current["config_rejected"] = rejected
            if (current.get("mode_pending") and state in {"applied", "in_sync"}
                    and info.get("xray") == "active"
                    and isinstance(info.get("reality_public"), bool)
                    and info["reality_public"] == (not current.get("private", True))):
                current["mode_pending"] = False
                changed = True
            if changed:
                save(stored)
            break


def online(node: dict) -> bool:
    r = reports.get(node["name"])
    return bool(r) and time.time() - r["seen"] < 180


def healthy(node: dict) -> bool:
    report = reports.get(node["name"], {})
    apply = report.get("config_apply") or {}
    return (online(node) and report.get("xray") == "active"
            and apply.get("state") not in {"rejected", "restart_failed"}
            and not node.get("mode_pending") and not node.get("config_rejected"))


def standby_bundle() -> dict:
    """Everything a node needs to take over as the main server: a consistent DB snapshot,
    the settings file (bot token, Reality keys) and the origin certificate."""
    import base64
    import os
    import sqlite3
    import tempfile
    from config import ENV_FILE
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src, dst = sqlite3.connect(cfg.db_path), sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        snap = open(tmp, "rb").read()
    finally:
        os.unlink(tmp)
    b64 = lambda p: base64.b64encode(open(p, "rb").read()).decode()  # noqa: E731
    return {"db": base64.b64encode(snap).decode(), "env": b64(ENV_FILE),
            "cert": b64(cfg.cert_file), "key": b64(cfg.key_file), "at": int(time.time())}


def cert_fingerprint() -> str:
    pem = open(cfg.cert_file).read()
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
