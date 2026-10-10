#!/usr/bin/env bash
# Secondary foreign server ("node"): Xray only. Pulls its config (same users/keys as the main
# server) from the bot every minute and reports traffic back.
#   usage: bash node.sh <main-url> <node-key> <main-cert-sha256> [cdn-domain] [relay-ips]
set -euo pipefail
MAIN=${1:?usage: bash node.sh <main-url> <node-key> <main-cert-sha256> [cdn-domain]}
KEY=${2:?node key missing}
PIN=${3:?main certificate fingerprint missing}
DOMAIN=${4:-node.local}
RELAYS=${5:-}
CONF_DIR=/etc/isaho-node
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq curl unzip openssl python3 ca-certificates nftables >/dev/null

echo "==> Xray"
# Must match the main server's pin: a node serves the same customers with the same REALITY keys,
# and from v26.9.8 the REALITY server rejects clients that do not offer X25519MLKEM768 first,
# which silently breaks every sing-box-based client. See vpn/ANTIFILTER.md §2.
XRAY_VERSION=${XRAY_VERSION:-v26.7.28}
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" \
    @ install -u root --version "$XRAY_VERSION" >/dev/null
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

echo "==> Direct proxy (port 8443 → Xray 443)"
cat >/etc/systemd/system/isaho-direct.socket <<SOCK
[Unit]
Description=IsaHo direct path from IR relays (8443 -> Xray 127.0.0.1:443)
Requires=isaho-direct-fw.service
After=isaho-direct-fw.service
[Socket]
ListenStream=0.0.0.0:8443
NoDelay=true
Backlog=4096
[Install]
WantedBy=sockets.target
SOCK
cat >/etc/systemd/system/isaho-direct.service <<SVC
[Unit]
Description=IsaHo direct path proxy to Xray
Requires=isaho-direct.socket
After=isaho-direct.socket
[Service]
ExecStart=/lib/systemd/systemd-socket-proxyd --connections-max=4096 127.0.0.1:443
LimitNOFILE=65536
SVC
if [[ $RELAYS =~ ^[0-9.,]+$ ]]; then
    cat >/etc/isaho-direct.nft <<NFT
table inet isaho_direct {
    chain input {
        type filter hook input priority filter - 10; policy accept;
        tcp dport 8443 ip saddr { ${RELAYS//,/ , } } accept
        tcp dport 8443 drop
    }
}
NFT
    chmod 600 /etc/isaho-direct.nft
    cat >/etc/systemd/system/isaho-direct-fw.service <<FW
[Unit]
Description=IsaHo direct-node port 8443 allowlist (relays only)
Before=network-pre.target isaho-direct.socket
Wants=network-pre.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/nft delete table inet isaho_direct
ExecStart=/usr/sbin/nft -f /etc/isaho-direct.nft
ExecStop=/usr/sbin/nft delete table inet isaho_direct
[Install]
WantedBy=multi-user.target
FW
else
    cat >/etc/isaho-direct.nft <<NFT
table inet isaho_direct {
    chain input {
        type filter hook input priority filter - 10; policy accept;
        tcp dport 8443 drop
    }
}
NFT
    chmod 600 /etc/isaho-direct.nft
    cat >/etc/systemd/system/isaho-direct-fw.service <<FW
[Unit]
Description=IsaHo direct-node port 8443 deny-all fallback
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/nft delete table inet isaho_direct
ExecStart=/usr/sbin/nft -f /etc/isaho-direct.nft
ExecStop=/usr/sbin/nft delete table inet isaho_direct
[Install]
WantedBy=multi-user.target
FW
fi
systemctl daemon-reload
systemctl enable -q isaho-direct-fw.service isaho-direct.socket
systemctl restart isaho-direct-fw.service
systemctl start isaho-direct.socket
# no public VPN ports by default: 8443 accepts only the supplied Iranian relay IPs
# (switch the node to public in the bot and open 443/2053 yourself if you ever want direct links)

echo "==> Sync agent"
cat >/usr/local/bin/isaho-node <<'AGENT'
#!/usr/bin/env python3
"""Pull the Xray config from the bot, apply it if it changed, and push traffic counters."""
import base64, hashlib, http.client, ipaddress, json, os, re, socket, ssl, subprocess, sys, time, urllib.parse

conf = dict(l.split("=", 1) for l in open("/etc/isaho-node/node.conf").read().split() if "=" in l)
url = urllib.parse.urlsplit(conf["MAIN"])
XRAY, CFG, PENDING = "/usr/local/bin/xray", "/usr/local/etc/xray/config.json", "/etc/isaho-node/pending.json"
RECOVERY_STAMP = "/etc/isaho-node/recovery-at"


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
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 124, "", "command unavailable or timed out")


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


def port_preflight(config):
    """Fail closed on foreign owners; only this service's current PID may be replaced."""
    pid = run("systemctl", "show", "xray", "--property=MainPID", "--value").stdout.strip()
    for inbound in config.get("inbounds", []):
        address = inbound.get("listen", "0.0.0.0")
        try:
            target = ipaddress.ip_address(address)
            port = int(inbound["port"])
            if not 1 <= port <= 65535:
                return "invalid inbound port"
        except (ValueError, TypeError, KeyError):
            return "unsupported inbound bind address or port"
        result = run("ss", "-Htlnp", f"sport = :{port}")
        if result.returncode:
            return f"cannot inspect port {port} ownership"
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4:
                return f"cannot parse port {port} ownership"
            bound = fields[3].rsplit(":", 1)[0].strip("[]").split("%", 1)[0]
            if bound != "*":
                try:
                    local = ipaddress.ip_address(bound)
                except ValueError:
                    return f"cannot parse port {port} bind address"
                # Exact disjoint addresses can coexist; wildcard binds are conservative.
                if not local.is_unspecified and not target.is_unspecified and local != target:
                    continue
            owners = set(re.findall(r"pid=(\d+)", line))
            if not owners or pid == "0" or owners != {pid}:
                return f"port {port} has a foreign or unknown owner"
    return ""


def apply_config(config, text, old):
    if text == old:
        return {"state": "in_sync", "reason": ""}
    tmp = CFG[:-len(".json")] + ".new.json"
    try:
        with open(tmp, "w") as output:
            output.write(text)
        os.chmod(tmp, 0o600)
        if run(XRAY, "run", "-test", "-c", tmp).returncode:
            reason = "new config failed validation"
        else:
            reason = port_preflight(config)
        if reason:
            print(reason + "; keeping the old config", file=sys.stderr)
            return {"state": "rejected", "reason": reason}
        global pending
        pending = merge(pending, stats())
        os.replace(tmp, CFG)
        result = run("systemctl", "restart", "xray")
        return {"state": "applied" if result.returncode == 0 else "restart_failed",
                "reason": "" if result.returncode == 0 else "Xray restart failed",
                "restart_code": result.returncode}
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def recover_failed():
    """Recover failed only, never inactive/activating; at most one attempt per 300s."""
    if run("systemctl", "is-active", "xray").stdout.strip() != "failed":
        return {"state": "not_needed"}
    stamp = RECOVERY_STAMP
    now = time.time()
    try:
        previous = 0
        if os.path.exists(stamp):
            with open(stamp) as source:
                previous = float(source.read())
    except (OSError, ValueError):
        return {"state": "blocked", "reason": "cannot read recovery stamp"}
    if now - previous < 300:
        return {"state": "backoff"}
    if run(XRAY, "run", "-test", "-c", CFG).returncode:
        return {"state": "blocked", "reason": "current config failed validation"}
    try:
        with open(CFG) as source:
            reason = port_preflight(json.load(source))
        if reason:
            return {"state": "blocked", "reason": reason}
        fd = os.open(stamp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(str(now))
    except (OSError, ValueError):
        return {"state": "blocked", "reason": "cannot prepare recovery"}
    # Do not compete with another operator or systemd's automatic restart.
    if run("systemctl", "is-active", "xray").stdout.strip() != "failed":
        return {"state": "state_changed"}
    start = run("systemctl", "start", "xray")
    result = {"state": "started" if start.returncode == 0 else "failed",
              "start_code": start.returncode}
    if start.returncode and run("systemctl", "is-active", "xray").stdout.strip() == "failed":
        reset = run("systemctl", "reset-failed", "xray")
        result["reset_code"] = reset.returncode
        if reset.returncode == 0:
            retry = run("systemctl", "start", "xray")
            result.update(state="started" if retry.returncode == 0 else "failed",
                          retry_code=retry.returncode)
    if result["state"] == "failed":
        print("bounded Xray recovery failed", file=sys.stderr)
    return result


pending = json.load(open(PENDING)) if os.path.exists(PENDING) else {}
pending = merge(pending, stats())

# Local recovery must not depend on the main bot being reachable.
recovery = recover_failed()
if recovery.get("state") == "blocked":
    print("Xray recovery blocked: " + recovery["reason"], file=sys.stderr)
new = request("GET", "/node/config")
text = json.dumps(new, indent=2, sort_keys=True)
old = open(CFG).read() if os.path.exists(CFG) else ""
config_apply = apply_config(new, text, old)
if config_apply["state"] == "restart_failed":
    recovery = recover_failed()
applied = json.load(open(CFG)) if os.path.exists(CFG) else {}
reality_public = any(i.get("tag") == "reality" and i.get("listen") not in
                     ("127.0.0.1", "::1") for i in applied.get("inbounds", []))

# five-minute standby copy of the bot so a promotion loses at most a few minutes of state
STANDBY = "/etc/isaho-node/standby"
stamp = os.path.join(STANDBY, "at")
if not os.path.exists(stamp) or time.time() - os.path.getmtime(stamp) > 300:
    try:
        b = request("GET", "/node/backup")
        os.makedirs(STANDBY, mode=0o700, exist_ok=True)
        for name, field in (("isaho.db", "db"), ("vpn.env", "env"), ("cert.pem", "cert"), ("key.pem", "key")):
            tmp = os.path.join(STANDBY, name + ".tmp")
            with open(tmp, "wb") as f:
                f.write(base64.b64decode(b[field]))
            os.chmod(tmp, 0o600)
            os.replace(tmp, os.path.join(STANDBY, name))
        open(stamp, "w").write(str(b.get("at", int(time.time()))))
    except SystemExit as e:
        print(f"standby copy failed: {e}", file=sys.stderr)

info = {"hostname": socket.gethostname(), "load": os.getloadavg()[0],
        "xray": run("systemctl", "is-active", "xray").stdout.strip(),
        "config_apply": config_apply, "recovery": recovery, "reality_public": reality_public,
        "standby_at": int(open(stamp).read()) if os.path.exists(stamp) else 0}
try:
    request("POST", "/node/stats", {"stats": pending, "info": info})
    pending = {}
finally:
    json.dump(pending, open(PENDING, "w"))
AGENT
chmod +x /usr/local/bin/isaho-node

cat >/usr/local/bin/isaho-takeover <<'TAKEOVER'
#!/usr/bin/env bash
# Promote this node to main server: run the bot here from the latest standby copy.
# Only when the main server is really gone - two bots with one token fight over updates.
set -euo pipefail
S=/etc/isaho-node/standby
[[ -f $S/isaho.db && -f $S/vpn.env ]] || { echo "no standby copy yet"; exit 1; }
[[ -f $S/at ]] || { echo "standby timestamp is missing"; exit 1; }
AGE=$(( $(date +%s) - $(cat "$S/at") ))
(( AGE <= 600 )) || { echo "standby copy is stale (${AGE}s); sync it before takeover"; exit 1; }
echo "Standby copy from: $(date -d @"$(cat $S/at)")"
if [[ ${1:-} != --yes ]]; then
    read -rp "Is the main server really down and should this server become the main one? [yes/N] " a </dev/tty
    [[ $a == yes ]] || exit 1
fi
MY_IP=$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<NF;i++) if($i=="src") print $(i+1)}')
mkdir -p /etc/isaho-vpn /var/lib/isaho-vpn && chmod 700 /etc/isaho-vpn /var/lib/isaho-vpn
sed "s/^SERVER_IP=.*/SERVER_IP=$MY_IP/" $S/vpn.env >/etc/isaho-vpn/vpn.env
cp $S/cert.pem $S/key.pem /etc/isaho-vpn/ && cp $S/isaho.db /var/lib/isaho-vpn/isaho.db
chmod 600 /etc/isaho-vpn/* /var/lib/isaho-vpn/isaho.db
systemctl disable --now isaho-node.timer
apt-get install -y -qq git >/dev/null
[[ -d /root/IsaHo/.git ]] || git clone -q https://github.com/IsaHo/IsaHo.git /root/IsaHo
git -C /root/IsaHo pull -q || true
bash /root/IsaHo/vpn/install.sh
echo "
✅ The bot now runs here ($MY_IP).
Next: on each Iranian relay run relay.sh ssh with THIS server first, e.g.
  relay.sh ssh $MY_IP 4
and run its AUTHORIZED line here. Point vpn.zkim.app at $MY_IP in Cloudflare if you use it."
TAKEOVER
chmod +x /usr/local/bin/isaho-takeover
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
