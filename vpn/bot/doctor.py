"""End-to-end self test: runs a real Xray client on the server and tries each config."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import db
import links
from config import cfg

TEST_URL = "https://www.gstatic.com/generate_204"


def client_config(port: int, outbound: dict) -> dict:
    return {"log": {"loglevel": "warning"},
            "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks"}],
            "outbounds": [outbound]}


def vless(address: str, port: int, uuid: str, stream: dict, flow: str = "") -> dict:
    user = {"id": uuid, "encryption": "none"}
    if flow:
        user["flow"] = flow
    return {"protocol": "vless", "settings": {"vnext": [{"address": address, "port": port, "users": [user]}]},
            "streamSettings": stream}


def try_config(name: str, port: int, outbound: dict) -> bool:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(client_config(port, outbound), f)
    proc = subprocess.Popen([cfg.xray_bin, "run", "-c", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(2)
    ok, detail = False, ""
    try:
        r = subprocess.run(["curl", "-sS", "-m", "15", "-o", "/dev/null", "-w", "%{http_code} %{time_total}s",
                            "--socks5-hostname", f"127.0.0.1:{port}", TEST_URL],
                           capture_output=True, text=True, env={k: v for k, v in os.environ.items()
                                                                if "proxy" not in k.lower()})
        detail = (r.stdout + r.stderr).strip()
        ok = r.stdout.startswith("204")
    finally:
        proc.terminate()
        out = proc.communicate(timeout=5)[0].decode(errors="replace")
        os.unlink(path)
    print(f"{'✅' if ok else '❌'} {name}: {detail}")
    if not ok:
        print("   xray client log:", out.strip()[-600:])
    return ok


def main() -> None:
    users = db.active_users()
    if not users:
        sys.exit("No active user. Create one in the bot first.")
    u = users[0]
    print(f"Testing with user '{u.name}'\n")

    try_config("Reality (direct to server IP)", 30801, vless(
        cfg.server_ip, cfg.reality_port, u.uuid, flow="xtls-rprx-vision", stream={
            "network": "tcp", "security": "reality",
            "realitySettings": {"serverName": cfg.reality_sni, "fingerprint": "chrome",
                                "publicKey": cfg.reality_public_key, "shortId": cfg.reality_short_id}}))

    try_config(f"CDN XHTTP (through Cloudflare, port {links.cdn_public_port()})", 30802, vless(
        links.cdn_address(), links.cdn_public_port(), u.uuid, stream={
            "network": "xhttp", "security": "tls",
            "tlsSettings": {"serverName": cfg.domain, "fingerprint": "chrome", "alpn": ["h2", "http/1.1"]},
            "xhttpSettings": {"host": cfg.domain, "path": cfg.cdn_path, "mode": "packet-up"}}))

    try:
        req = urllib.request.Request(links.sub_url(u), headers={"User-Agent": "v2rayNG/1.9"})
        with urllib.request.urlopen(req, timeout=15) as r:
            n = len(links.all_links(u))
            print(f"✅ Subscription through Cloudflare: HTTP {r.status}, {n} configs")
    except Exception as e:
        print(f"❌ Subscription through Cloudflare: {e}")

    print("\nIf everything above is ✅ the server side is fine and any failure is on the client network/app.")


if __name__ == "__main__":
    main()
