"""Client share links and subscription helpers."""
import base64
from urllib.parse import quote, urlencode

import db
from config import cfg


def cdn_address() -> str:
    """Address clients dial for the CDN config: a clean Cloudflare IP if set, else the domain."""
    return db.get_setting("cdn_address") or cfg.domain


def cdn_public_port() -> int:
    """Port clients dial on Cloudflare. 443 needs an Origin Rule rewriting it to CDN_PORT."""
    return int(db.get_setting("cdn_public_port") or cfg.cdn_port)


def relays() -> list:
    """Iranian relay servers as (host, port); they NAT-forward to our Reality port."""
    out = []
    for item in db.get_setting("relays").split(","):
        item = item.strip()
        if item:
            host, _, port = item.rpartition(":") if ":" in item else (item, "", "443")
            out.append((host, int(port)))
    return out


def reality_link(u, address: str = "", port: int = 0, tag: str = "Reality") -> str:
    q = urlencode({
        "encryption": "none", "flow": "xtls-rprx-vision", "security": "reality",
        "sni": cfg.reality_sni, "fp": "chrome", "pbk": cfg.reality_public_key,
        "sid": cfg.reality_short_id, "type": "tcp", "headerType": "none",
    })
    address, port = address or cfg.server_ip, port or cfg.reality_port
    return f"vless://{u.uuid}@{address}:{port}?{q}#{quote(f'{cfg.brand}-{u.name}-{tag}')}"


def cdn_link(u, domain: str = "", tag: str = "CDN") -> str:
    domain = domain or cfg.domain
    q = urlencode({
        "encryption": "none", "security": "tls", "sni": domain, "fp": "chrome",
        "alpn": "h2,http/1.1", "type": "xhttp", "host": domain, "path": cfg.cdn_path,
        "mode": "packet-up",
    })
    address = cdn_address() if domain == cfg.domain else domain
    return f"vless://{u.uuid}@{address}:{cdn_public_port()}?{q}#{quote(f'{cfg.brand}-{u.name}-{tag}')}"


LINK_TYPES = {"relay": "🇮🇷 تانل (سرور واسط)", "reality": "⚡ Reality مستقیم", "cdn": "☁️ CDN",
              "node": "🌍 سرورهای خارج دیگر (مستقیم + CDN)"}


def enabled_types() -> set:
    """Link types included in configs and subscriptions. Defaults to the tunnel only once a relay exists."""
    raw = db.get_setting("link_types", "")
    if raw:
        return {t for t in raw.split(",") if t in LINK_TYPES}
    return {"relay"} if relays() else {"reality", "cdn"}


def toggle_type(t: str) -> set:
    types = enabled_types() ^ {t}
    db.set_setting("link_types", ",".join(sorted(types)) or "none")
    return types


def all_links(u) -> list:
    types, links = enabled_types(), []
    if "relay" in types and cfg.reality_public_key:
        for i, (host, port) in enumerate(relays(), 1):
            links.append(reality_link(u, host, port, f"IR{i}"))
    if "reality" in types and cfg.server_ip and cfg.reality_public_key:
        links.append(reality_link(u))
    if "cdn" in types and cfg.domain:
        links.append(cdn_link(u))
    if "node" in types and cfg.reality_public_key:
        import nodes
        for n in nodes.all_nodes():
            links.append(reality_link(u, n["ip"], cfg.reality_port, f"{n['name']}-Reality"))
            if n.get("domain"):
                links.append(cdn_link(u, n["domain"], f"{n['name']}-CDN"))
    return links


def cdn_sub_url(u) -> str:
    return f"https://{cfg.domain}:{cfg.sub_port}/sub/{u.sub_token}"


def backup_sub_urls(u) -> list:
    """Subscription through the other relays, in case the first one is down."""
    if "relay" not in enabled_types():
        return []
    return [f"http://{host}:2096/sub/{u.sub_token}" for host, _ in relays()[1:]]


def sub_url(u) -> str:
    """Prefer the first Iranian relay (reachable from inside Iran); fall back to Cloudflare."""
    if relays() and "relay" in enabled_types():
        return f"http://{relays()[0][0]}:2096/sub/{u.sub_token}"
    return cdn_sub_url(u)


def sub_body(u) -> str:
    return base64.b64encode("\n".join(all_links(u)).encode()).decode()


def sub_userinfo(u) -> str:
    return f"upload={u.up}; download={u.down}; total={u.traffic_limit}; expire={u.expire_at}"
