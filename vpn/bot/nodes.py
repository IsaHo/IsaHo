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


def toggle_private(name: str) -> None:
    nodes = all_nodes()
    for n in nodes:
        if n["name"] == name:
            n["private"] = not n.get("private", True)
    save(nodes)


def public_nodes() -> list:
    return [n for n in all_nodes() if not n.get("private", True)]


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


def online(node: dict) -> bool:
    r = reports.get(node["name"])
    return bool(r) and time.time() - r["seen"] < 180


def cert_fingerprint() -> str:
    pem = open(cfg.cert_file).read()
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
