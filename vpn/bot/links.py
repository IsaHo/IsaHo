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


def reality_link(u) -> str:
    q = urlencode({
        "encryption": "none", "flow": "xtls-rprx-vision", "security": "reality",
        "sni": cfg.reality_sni, "fp": "chrome", "pbk": cfg.reality_public_key,
        "sid": cfg.reality_short_id, "type": "tcp", "headerType": "none",
    })
    return f"vless://{u.uuid}@{cfg.server_ip}:{cfg.reality_port}?{q}#{quote(f'{cfg.brand}-{u.name}-Reality')}"


def cdn_link(u) -> str:
    q = urlencode({
        "encryption": "none", "security": "tls", "sni": cfg.domain, "fp": "chrome",
        "alpn": "h2,http/1.1", "type": "xhttp", "host": cfg.domain, "path": cfg.cdn_path,
        "mode": "packet-up",
    })
    return f"vless://{u.uuid}@{cdn_address()}:{cdn_public_port()}?{q}#{quote(f'{cfg.brand}-{u.name}-CDN')}"


def all_links(u) -> list:
    links = []
    if cfg.server_ip and cfg.reality_public_key:
        links.append(reality_link(u))
    if cfg.domain:
        links.append(cdn_link(u))
    return links


def sub_url(u) -> str:
    return f"https://{cfg.domain}:{cfg.sub_port}/sub/{u.sub_token}"


def sub_body(u) -> str:
    return base64.b64encode("\n".join(all_links(u)).encode()).decode()


def sub_userinfo(u) -> str:
    return f"upload={u.up}; download={u.down}; total={u.traffic_limit}; expire={u.expire_at}"
