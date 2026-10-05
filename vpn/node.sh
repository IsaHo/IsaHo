#!/usr/bin/env bash
# Secondary foreign server ("node"): Xray only. Pulls its config (same users/keys as the main
# server) from the bot every minute and reports traffic back.
#   usage: bash node.sh <main-url e.g. https://1.2.3.4:2096> <node-key> <main-cert-sha256> [cdn-domain]
set -euo pipefail
MAIN=${1:?usage: bash node.sh <main-url> <node-key> <main-cert-sha256> [cdn-domain]}
KEY=${2:?node key missing}
PIN=${3:?main certificate fingerprint missing}
DOMAIN=${4:-node.local}
CONF_DIR=/etc/isaho-node
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq curl unzip openssl python3 ca-certificates >/dev/null

echo "==> Xray"
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install -u root >/dev/null
mkdir -p /etc/systemd/system/xray.service.d
printf '[Service]\nLimitNOFILE=1048576\n' >/etc/systemd/system/xray.service.d/isaho.conf

mkdir -p "$CONF_DIR" && chmod 700 "$CONF_DIR"
if [[ ! -f $CONF_DIR/cert.pem ]]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
        -keyout "$CONF_DIR/key.pem" -out "$CONF_DIR/cert.pem" -subj "/CN=$DOMAIN" \
        -addext "subjectAltName=DNS:$DOMAIN" 2>/dev/null
fi
cat >"$CONF_DIR/node.conf" <<CONF
MAIN=$MAIN
KEY=$KEY
PIN=$PIN
CONF
chmod 600 "$CONF_DIR/node.conf"

echo "==> Network tuning"
cat >/etc/sysctl.d/99-isaho.conf <<SYSCTL
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.ipv4.tcp_fastopen=3
net.core.rmem_max=67108864
net.core.wmem_max=67108864
net.ipv4.tcp_rmem=4096 87380 67108864
net.ipv4.tcp_wmem=4096 65536 67108864
net.ipv4.tcp_mtu_probing=1
net.ipv4.tcp_slow_start_after_idle=0
fs.file-max=1048576
SYSCTL
sysctl --system >/dev/null 2>&1 || true
grep -q '^precedence ::ffff:0:0/96' /etc/gai.conf 2>/dev/null || echo 'precedence ::ffff:0:0/96  100' >>/etc/gai.conf
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 443/tcp >/dev/null; ufw allow 2053/tcp >/dev/null
fi

echo "==> Sync agent"
cat >/usr/local/bin/isaho-node <<'AGENT'
#!/usr/bin/env python3
"""Pull the Xray config from the bot, apply it if it changed, and push traffic counters."""
import hashlib, http.client, json, os, socket, ssl, subprocess, sys, urllib.parse

conf = dict(l.split("=", 1) for l in open("/etc/isaho-node/node.conf").read().split() if "=" in l)
url = urllib.parse.urlsplit(conf["MAIN"])
XRAY, CFG, PENDING = "/usr/local/bin/xray", "/usr/local/etc/xray/config.json", "/etc/isaho-node/pending.json"


def request(method, path, body=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # self-signed: pinned below instead
    c = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=20, context=ctx)
    c.connect()
    if hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest() != conf["PIN"]:
        raise SystemExit("main server certificate does not match the pinned fingerprint")
    q = f"{path}?key={urllib.parse.quote(conf['KEY'])}"
    c.request(method, q, body=json.dumps(body).encode() if body is not None else None,
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    if r.status != 200:
        raise SystemExit(f"{path}: HTTP {r.status}")
    return json.loads(data)


def run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def stats():
    r = run(XRAY, "api", "statsquery", "--server=127.0.0.1:10085", "-pattern", "user>>>", "-reset")
    out = {}
    if r.returncode != 0:
        return out
    for s in (json.loads(r.stdout or "{}").get("stat") or []):
        p = s.get("name", "").split(">>>")
        if len(p) == 4:
            up, down = out.get(p[1], [0, 0])
            v = int(s.get("value", 0) or 0)
            out[p[1]] = [up + v, down] if p[3] == "uplink" else [up, down + v]
    return out


def merge(a, b):
    for k, (u, d) in b.items():
        a[k] = [a.get(k, [0, 0])[0] + u, a.get(k, [0, 0])[1] + d]
    return a


pending = json.load(open(PENDING)) if os.path.exists(PENDING) else {}
pending = merge(pending, stats())

new = request("GET", "/node/config")
text = json.dumps(new, indent=2, sort_keys=True)
old = open(CFG).read() if os.path.exists(CFG) else ""
if text != old:
    tmp = CFG[:-len(".json")] + ".new.json"  # xray picks the format from the extension
    open(tmp, "w").write(text)
    if run(XRAY, "run", "-test", "-c", tmp).returncode == 0:
        pending = merge(pending, stats())  # counters are lost on restart
        os.replace(tmp, CFG)
        run("systemctl", "restart", "xray")
    else:
        print("new config failed validation; keeping the old one", file=sys.stderr)

info = {"hostname": socket.gethostname(), "load": os.getloadavg()[0],
        "xray": run("systemctl", "is-active", "xray").stdout.strip()}
try:
    request("POST", "/node/stats", {"stats": pending, "info": info})
    pending = {}
finally:
    json.dump(pending, open(PENDING, "w"))
AGENT
chmod +x /usr/local/bin/isaho-node
cat >/etc/systemd/system/isaho-node.service <<UNIT
[Unit]
Description=IsaHo node sync
[Service]
Type=oneshot
ExecStart=/usr/local/bin/isaho-node
UNIT
cat >/etc/systemd/system/isaho-node.timer <<UNIT
[Unit]
Description=IsaHo node sync every minute
[Timer]
OnBootSec=20
OnUnitActiveSec=60
AccuracySec=5
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable -q xray
systemctl enable -q --now isaho-node.timer

echo "==> First sync"
if /usr/local/bin/isaho-node; then
    sleep 2
    systemctl is-active -q xray && echo "✅ Node is up and synced with the bot." || { journalctl -u xray -n 20 --no-pager; exit 1; }
else
    echo "✖ Could not reach the bot at $MAIN (check that port 2096 is open on the main server)"; exit 1
fi
