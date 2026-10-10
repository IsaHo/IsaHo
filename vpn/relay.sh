#!/usr/bin/env bash
# Turns an Iranian VPS into a relay: TCP on LISTEN_PORT is forwarded (kernel NAT, no
# decryption) to the foreign server's Reality port. Users dial the Iranian IP; per-user
# accounting keeps working on the foreign server because the protocol is untouched.
#   usage: bash relay.sh <foreign-server-ip> [listen-port] [foreign-port]
set -euo pipefail

# Test mode: bash relay.sh test '<vless link>' [override-address]
# Runs a real Xray client on this server and fetches a URL through the link.
if [[ ${1:-} == test ]]; then
    LINK=${2:?usage: bash relay.sh test '<vless link>' [address]}
    [[ $LINK == vless://* ]] || { echo "✖ the second argument must be a vless:// link (copy it from the bot)"; exit 1; }
    OVERRIDE=${3:-}
    D=/tmp/isaho-test; mkdir -p "$D"
    # Pinned to the same release the foreign server runs, so a probe reproduces what a
    # customer's client does rather than whatever "latest" happens to be (see vpn/ANTIFILTER.md §2).
    XRAY_VERSION=${XRAY_VERSION:-v26.7.28}
    if [[ ! -x $D/xray ]]; then
        command -v unzip >/dev/null || apt-get install -y -qq unzip >/dev/null
        curl -fsSL -o "$D/x.zip" "https://github.com/XTLS/Xray-core/releases/download/$XRAY_VERSION/Xray-linux-64.zip"
        unzip -oq "$D/x.zip" -d "$D"
    fi
    python3 - "$LINK" "$OVERRIDE" >"$D/c.json" <<'PY'
import json, sys
from urllib.parse import urlsplit, parse_qs, unquote
u = urlsplit(sys.argv[1]); q = {k: v[0] for k, v in parse_qs(u.query).items()}
host = sys.argv[2] or u.hostname
user = {"id": unquote(u.username), "encryption": "none"}
if q.get("flow"): user["flow"] = q["flow"]
stream = {"network": q.get("type", "tcp"), "security": q.get("security", "none")}
if stream["security"] == "reality":
    stream["realitySettings"] = {"serverName": q.get("sni"), "fingerprint": q.get("fp", "chrome"),
                                 "publicKey": q.get("pbk"), "shortId": q.get("sid", "")}
elif stream["security"] == "tls":
    stream["tlsSettings"] = {"serverName": q.get("sni"), "fingerprint": q.get("fp", "chrome"),
                             "alpn": q.get("alpn", "h2,http/1.1").split(",")}
if stream["network"] == "xhttp":
    stream["xhttpSettings"] = {"host": q.get("host", ""), "path": q.get("path", "/"), "mode": q.get("mode", "auto")}
print(json.dumps({"log": {"loglevel": "warning"},
                  "inbounds": [{"listen": "127.0.0.1", "port": 31999, "protocol": "socks"}],
                  "outbounds": [{"protocol": "vless", "settings": {"vnext": [
                      {"address": host, "port": u.port, "users": [user]}]}, "streamSettings": stream}]}))
PY
    "$D/xray" run -c "$D/c.json" >"$D/log" 2>&1 & XP=$!
    sleep 2
    if curl -sS -m 15 -o /dev/null -w "HTTP %{http_code} in %{time_total}s\n" --socks5-hostname 127.0.0.1:31999 https://www.gstatic.com/generate_204; then
        echo "✅ tunnel works from this server"
        URL="https://speed.cloudflare.com/__down?bytes=25000000"
        for i in 1 2 3; do
            curl -sS -m 15 -o /dev/null -w "   new connection #$i: %{time_appconnect}s handshake, %{time_total}s total\n" \
                --socks5-hostname 127.0.0.1:31999 https://www.gstatic.com/generate_204 || true
        done
        curl -sS -m 40 -o /dev/null -w "   download THROUGH tunnel: %{speed_download} B/s\n" --socks5-hostname 127.0.0.1:31999 "$URL" || true
        curl -sS -m 40 -o /dev/null -w "   download DIRECT from this server: %{speed_download} B/s\n" "$URL" || true
    else
        echo "❌ tunnel failed from this server"; tail -5 "$D/log"
    fi
    kill $XP 2>/dev/null
    exit 0
fi

# SSH mode: bash relay.sh ssh <foreign-ip> [tunnels] [ssh-port]
# Users' Reality connections ride inside several persistent SSH connections (port 22) to the
# foreign server, load-balanced by HAProxy with health checks, for networks where TLS to
# foreign IPs is throttled but SSH passes. The subscription is served here too, over HTTP :2096.
if [[ ${1:-} == ssh ]]; then
    # several foreign servers: "main-ip,node-ip,...". The first one hosts the bot (subscription,
    # agent reports); all of them carry user traffic.
    FOREIGN_LIST=${2:?usage: bash relay.sh ssh <foreign-ip[,node-ip...]> [tunnels] [ssh-port]}
    IFS=, read -ra FOREIGNS <<<"$FOREIGN_LIST"
    FOREIGN_IP=${FOREIGNS[0]}
    TUNNELS=${3:-3}
    SSH_PORT=${4:-22}
    LISTEN_PORT=443
    SUB_PORT=2096
    [[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
    [[ $TUNNELS =~ ^[1-8]$ ]] || { echo "tunnels must be 1-8"; exit 1; }

    echo "==> Installing HAProxy"
    command -v haproxy >/dev/null || { apt-get update -qq && apt-get install -y -qq haproxy >/dev/null; }
    command -v haproxy >/dev/null || { echo "✖ could not install haproxy (apt mirror?)"; exit 1; }

    # older relay styles would grab the same ports
    systemctl disable --now isaho-relay isaho-ssh-tunnel 2>/dev/null || true
    rm -f /etc/systemd/system/isaho-ssh-tunnel.service
    nft delete table ip isaho_relay 2>/dev/null || true
    for i in $(seq 1 8); do systemctl disable --now "isaho-tunnel@$i" 2>/dev/null || true; done
    for u in /etc/systemd/system/isaho-ntunnel-*.service; do
        [[ -e $u ]] || continue
        systemctl disable --now "$(basename "$u")" 2>/dev/null || true
        rm -f "$u"
    done

    KEY=/root/.ssh/isaho_tunnel
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    [[ -f $KEY ]] || ssh-keygen -q -t ed25519 -N "" -C isaho-relay -f "$KEY"

    cat >/etc/systemd/system/isaho-tunnel@.service <<UNIT
[Unit]
Description=IsaHo SSH tunnel #%i to $FOREIGN_IP
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N -T -i $KEY -p $SSH_PORT \\
  -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o TCPKeepAlive=yes \\
  -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes \\
  -o Compression=no -o IPQoS=throughput -o ConnectTimeout=10 \\
  -L 127.0.0.1:1000%i:127.0.0.1:443 -L 127.0.0.1:1100%i:127.0.0.1:2097 isaho-tunnel@$FOREIGN_IP
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

    SEND_PROXY=""
    grep -q "send-proxy-v2" /etc/haproxy/haproxy.cfg 2>/dev/null && SEND_PROXY=" send-proxy-v2 check-send-proxy"
    [[ -f /etc/haproxy/haproxy.cfg.orig ]] || cp /etc/haproxy/haproxy.cfg /etc/haproxy/haproxy.cfg.orig 2>/dev/null || true
    {
        cat <<CFG
global
    log /dev/log local0 warning
    maxconn 50000

defaults
    mode tcp
    log global
    timeout connect 5s
    timeout client 2h
    timeout server 2h
    option tcpka

frontend vpn
    bind :$LISTEN_PORT
    default_backend tunnels

backend tunnels
    balance leastconn
    # a plain TCP check would pass even when Xray on the far side is down (ssh accepts the local
    # connection first); a TLS hello must get a real TLS answer back through the tunnel
    option ssl-hello-chk
    default-server on-marked-down shutdown-sessions
CFG
        for i in $(seq 1 "$TUNNELS"); do echo "    server t$i 127.0.0.1:1000$i check inter 5s fall 2 rise 2$SEND_PROXY"; done
        # extra foreign servers: direct path on port 8443 (no SSH overhead) first,
        # SSH tunnels as backup so they only carry traffic when the direct path is down
        for j in $(seq 1 $(( ${#FOREIGNS[@]} - 1 ))); do
            echo "    server d${j}_1 ${FOREIGNS[$j]}:8443 check inter 5s fall 2 rise 2 weight $TUNNELS"
            for i in $(seq 1 "$TUNNELS"); do echo "    server n${j}_$i 127.0.0.1:$((12000 + j * 10 + i)) check inter 5s fall 2 rise 2 backup"; done
        done
        cat <<CFG

frontend sub
    bind :$SUB_PORT
    default_backend sub

backend sub
    balance first
CFG
        for i in $(seq 1 "$TUNNELS"); do echo "    server s$i 127.0.0.1:1100$i check inter 10s fall 2 rise 1"; done
    } >/etc/haproxy/haproxy.cfg
    haproxy -c -f /etc/haproxy/haproxy.cfg >/dev/null || { echo "✖ invalid haproxy config"; exit 1; }

    cat >/etc/sysctl.d/99-isaho-relay.conf <<SYSCTL
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.core.rmem_max=67108864
net.core.wmem_max=67108864
net.ipv4.tcp_rmem=4096 87380 67108864
net.ipv4.tcp_wmem=4096 65536 67108864
net.ipv4.tcp_slow_start_after_idle=0
SYSCTL
    sysctl --system >/dev/null 2>&1 || true
    if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
        ufw allow "$LISTEN_PORT/tcp" >/dev/null; ufw allow "$SUB_PORT/tcp" >/dev/null
    fi

    systemctl daemon-reload
    for i in $(seq 1 "$TUNNELS"); do systemctl enable -q "isaho-tunnel@$i"; systemctl restart "isaho-tunnel@$i"; done
    for j in $(seq 1 $(( ${#FOREIGNS[@]} - 1 ))); do
        for i in $(seq 1 "$TUNNELS"); do
            unit=isaho-ntunnel-$j-$i.service
            cat >/etc/systemd/system/$unit <<UNIT
[Unit]
Description=IsaHo SSH tunnel #$i to ${FOREIGNS[$j]}
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N -T -i $KEY -p $SSH_PORT \\
  -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o TCPKeepAlive=yes \\
  -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes \\
  -o Compression=no -o IPQoS=throughput -o ConnectTimeout=10 \\
  -L 127.0.0.1:$((12000 + j * 10 + i)):127.0.0.1:443 isaho-tunnel@${FOREIGNS[$j]}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
            systemctl daemon-reload
            systemctl enable -q "$unit"; systemctl restart "$unit"
        done
    done
    systemctl enable -q haproxy
    systemctl restart haproxy

    echo "==> Installing the status agent (reports to the bot through the tunnels)"
    # identity = the address users dial (the interface IP); egress = what the foreign server sees.
    # They differ on providers that NAT outgoing traffic through another IP.
    IFACE_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src") print $(i+1)}')
    EGRESS_IP=$(curl -4 -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)
    [[ $EGRESS_IP =~ ^[0-9.]+$ ]] || EGRESS_IP=$IFACE_IP
    PUBLIC_IP=$IFACE_IP
    [[ $IFACE_IP =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) || -z $IFACE_IP ]] && PUBLIC_IP=$EGRESS_IP
    cat >/etc/isaho-relay.conf <<CONF
FOREIGN_IP=$FOREIGN_LIST
TUNNELS=$TUNNELS
SSH_PORT=$SSH_PORT
PUBLIC_IP=$PUBLIC_IP
EGRESS_IP=$EGRESS_IP
XRAY_VERSION=${XRAY_VERSION:-v26.7.28}
VERSION=${ISAHO_REF:-manual}
CONF
    cat >/usr/local/bin/isaho-agent <<'AGENT'
#!/usr/bin/env python3
"""Reports this relay's health to the bot (through the SSH tunnels) and runs queued actions."""
import concurrent.futures, json, os, re, socket, subprocess, tempfile, time, urllib.parse, urllib.request, zipfile

conf = dict(l.split("=", 1) for l in open("/etc/isaho-relay.conf").read().split() if "=" in l)

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()

def net_bytes():
    rx = tx = 0
    for line in open("/proc/net/dev").read().splitlines()[2:]:
        name, data = line.split(":", 1)
        if name.strip() != "lo":
            f = data.split()
            rx += int(f[0]); tx += int(f[8])
    return rx, tx

PROBE_DIR = "/var/lib/isaho-agent"
PROBE_RESULT = os.path.join(PROBE_DIR, "probe-result.json")
PROBE_XRAY = os.path.join(PROBE_DIR, "xray")
# Same release the foreign server is pinned to; "latest" would silently change what a probe
# measures relative to what customers run. Overridable from /etc/isaho-relay.conf.
PROBE_XRAY_VERSION = conf.get("XRAY_VERSION", "v26.7.28")

def ensure_probe_xray():
    if os.path.exists(PROBE_XRAY):
        return PROBE_XRAY
    os.makedirs(PROBE_DIR, mode=0o700, exist_ok=True)
    archive = os.path.join(PROBE_DIR, "xray.zip")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = ("https://github.com/XTLS/Xray-core/releases/download/"
           f"{PROBE_XRAY_VERSION}/Xray-linux-64.zip")
    with opener.open(url, timeout=60) as response, open(archive, "wb") as out:
        out.write(response.read())
    with zipfile.ZipFile(archive) as package:
        with package.open("xray") as source, open(PROBE_XRAY, "wb") as out:
            out.write(source.read())
    os.chmod(PROBE_XRAY, 0o700)
    os.unlink(archive)
    return PROBE_XRAY

def probe_outbound(link):
    parsed = urllib.parse.urlsplit(link)
    query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
    user = {"id": urllib.parse.unquote(parsed.username), "encryption": "none"}
    if query.get("flow"):
        user["flow"] = query["flow"]
    stream = {"network": query.get("type", "tcp"), "security": query.get("security", "none")}
    if stream["security"] == "reality":
        stream["realitySettings"] = {
            "serverName": query.get("sni"), "fingerprint": query.get("fp", "chrome"),
            "publicKey": query.get("pbk"), "shortId": query.get("sid", ""),
        }
    elif stream["security"] == "tls":
        stream["tlsSettings"] = {
            "serverName": query.get("sni") or query.get("host", ""),
            "fingerprint": query.get("fp", "chrome"),
            "alpn": [item for item in query.get("alpn", "").split(",") if item],
        }
    if stream["network"] == "xhttp":
        stream["xhttpSettings"] = {
            "host": query.get("host", ""),
            "path": query.get("path", "/"),
            "mode": query.get("mode", "auto"),
        }
    return {"protocol": "vless", "settings": {"vnext": [{
        "address": parsed.hostname, "port": parsed.port, "users": [user]
    }]}, "streamSettings": stream}

def probe_vless(link):
    process, config_path = None, None
    started = time.monotonic()
    try:
        binary = ensure_probe_xray()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        fd, config_path = tempfile.mkstemp(prefix="probe-", suffix=".json", dir=PROBE_DIR)
        config = {"log": {"loglevel": "warning"},
                  "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks"}],
                  "outbounds": [probe_outbound(link)]}
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream)
        os.chmod(config_path, 0o600)
        process = subprocess.Popen([binary, "run", "-c", config_path], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        check = subprocess.run(
            ["curl", "-sS", "--connect-timeout", "5", "-m", "12", "-o", "/dev/null",
             "-w", "%{http_code} %{time_total}", "--socks5-hostname", f"127.0.0.1:{port}",
             "https://www.gstatic.com/generate_204"], capture_output=True, text=True, timeout=15,
            env={key: value for key, value in os.environ.items() if "proxy" not in key.lower()})
        fields = check.stdout.strip().split()
        code = fields[0] if fields else "000"
        latency = round(float(fields[1]) * 1000) if len(fields) > 1 else round((time.monotonic() - started) * 1000)
        ok = check.returncode == 0 and code == "204"
        return {"ok": ok, "latency_ms": latency,
                "detail": f"HTTP {code}" if ok else (check.stderr.strip() or f"HTTP {code}")[:200]}
    except Exception as exc:
        return {"ok": False, "latency_ms": round((time.monotonic() - started) * 1000),
                "detail": type(exc).__name__}
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        try:
            if config_path:
                os.unlink(config_path)
        except OSError:
            pass

def run_probe(job):
    result = {"job_id": str(job.get("id", ""))[:80], "checked_at": int(time.time())}
    started = time.monotonic()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(str(job["sub_url"]), timeout=12) as response:
            ok = response.status == 200
        result["sub"] = {"ok": ok, "latency_ms": round((time.monotonic() - started) * 1000),
                         "detail": f"HTTP {response.status}"}
    except Exception as exc:
        result["sub"] = {"ok": False, "latency_ms": round((time.monotonic() - started) * 1000),
                         "detail": type(exc).__name__}
    # Download the trusted diagnostic binary once before starting bounded workers.
    ensure_probe_xray()
    result["nodes"] = {}
    result["paths"] = {}
    tasks = [("vpn", "", str(job["link"]))]
    if job.get("cdn_link"):
        tasks.append(("cdn", "", str(job["cdn_link"])))
    node_jobs = job.get("nodes") if isinstance(job.get("nodes"), list) else []
    for item in node_jobs[:8]:
        if isinstance(item, dict) and item.get("name") and item.get("link"):
            tasks.append(("nodes", str(item["name"])[:32], str(item["link"])))
    paths = job.get("paths") if isinstance(job.get("paths"), dict) else {}
    for key, link in list(paths.items())[:16]:
        if isinstance(link, str) and link.startswith("vless://"):
            tasks.append(("paths", str(key)[:64], link))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [(group, key, pool.submit(probe_vless, link)) for group, key, link in tasks]
        for group, key, future in futures:
            if key:
                result[group][key] = future.result()
            else:
                result[group] = future.result()
    return result

mem = dict((l.split(":")[0], int(l.split()[1])) for l in open("/proc/meminfo"))
rx, tx = net_bytes()
report = {
    "ip": conf.get("PUBLIC_IP") or socket.gethostname(),
    "egress": conf.get("EGRESS_IP", ""),
    "hostname": socket.gethostname(),
    "version": conf.get("VERSION", "?"),
    "tunnels_total": int(conf.get("TUNNELS", 0)),
    "tunnels_up": int(sh("systemctl list-units 'isaho-tunnel@*' --state=active --no-legend --plain | wc -l") or 0),
    "node_tunnels_up": int(sh("systemctl list-units 'isaho-ntunnel-*' --state=active --no-legend --plain | wc -l") or 0),
    "node_tunnels_total": int(conf.get("TUNNELS", 0)) * (len(conf.get("FOREIGN_IP", "").split(",")) - 1),
    "haproxy": sh("systemctl is-active haproxy"),
    "load": os.getloadavg()[0],
    "mem": round(100 * (1 - mem.get("MemAvailable", 0) / max(mem.get("MemTotal", 1), 1))),
    "rx": rx, "tx": tx,
    "uptime": int(float(open("/proc/uptime").read().split()[0])),
    "last": sh("tail -n 1 /var/log/isaho-update.log 2>/dev/null"),
    "agent": 4,
    "proxy": "send-proxy-v2" in open("/etc/haproxy/haproxy.cfg").read(),
}
try:
    if os.path.exists(PROBE_RESULT):
        report["probe_result"] = json.load(open(PROBE_RESULT))
except (OSError, ValueError):
    pass
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
req = urllib.request.Request("http://127.0.0.1:2096/relay/report", data=json.dumps(report).encode(),
                             headers={"Content-Type": "application/json"})
try:
    reply = json.load(opener.open(req, timeout=15))
except Exception as e:
    raise SystemExit(f"report failed: {e}")
if "probe_result" in report:
    try:
        os.unlink(PROBE_RESULT)
    except OSError:
        pass

job = reply.get("probe")
if isinstance(job, dict) and job.get("id") and job.get("link") and job.get("sub_url"):
    try:
        os.makedirs(PROBE_DIR, mode=0o700, exist_ok=True)
        with open(PROBE_RESULT + ".tmp", "w") as stream:
            json.dump(run_probe(job), stream)
        os.replace(PROBE_RESULT + ".tmp", PROBE_RESULT)
    except Exception:
        pass

# the foreign server tells us whether it expects the PROXY header (real client IPs)
want = reply.get("proxy")
if isinstance(want, bool) and want != report["proxy"]:
    path = "/etc/haproxy/haproxy.cfg"
    lines = open(path).read().splitlines()
    out = []
    for line in lines:
        if line.strip().startswith("server t"):
            line = line.replace(" check-send-proxy", "").replace(" send-proxy-v2", "")
            line += " send-proxy-v2 check-send-proxy" if want else ""
        out.append(line)
    open(path + ".new", "w").write("\n".join(out) + "\n")
    if subprocess.run(["haproxy", "-c", "-f", path + ".new"], capture_output=True).returncode == 0:
        os.replace(path + ".new", path)
        sh("systemctl reload haproxy")

action, ref = reply.get("action"), reply.get("ref", "")
if action == "restart":
    sh("systemctl restart 'isaho-tunnel@*' haproxy")
elif action == "update" and re.fullmatch(r"[0-9a-f]{7,40}|main", ref):
    script = (f"curl -fsSL https://raw.githubusercontent.com/IsaHo/IsaHo/{ref}/vpn/relay.sh | "
              f"ISAHO_REF={ref} bash -s ssh {conf['FOREIGN_IP']} {conf['TUNNELS']} {conf['SSH_PORT']} "
              f"> /var/log/isaho-update.log 2>&1; echo \"$(date '+%F %T') update to {ref[:10]} exit=$?\" >> /var/log/isaho-update.log")
    subprocess.run(["systemd-run", "--unit", f"isaho-update-{int(time.time())}", "bash", "-c", script])
AGENT
    chmod +x /usr/local/bin/isaho-agent
    cat >/etc/systemd/system/isaho-agent.service <<UNIT
[Unit]
Description=IsaHo relay status agent

[Service]
Type=oneshot
ExecStart=/usr/local/bin/isaho-agent
TimeoutStartSec=300
UNIT
    cat >/etc/systemd/system/isaho-agent.timer <<UNIT
[Unit]
Description=Run the IsaHo relay agent every minute

[Timer]
OnBootSec=40
OnUnitActiveSec=60
AccuracySec=5

[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload
    systemctl enable -q --now isaho-agent.timer

    PUB=$(cat "$KEY.pub")
    echo "
✅ $TUNNELS SSH tunnels + HAProxy installed. Users: :$LISTEN_PORT   Subscription: http://<this-ip>:$SUB_PORT

Run THIS once on EACH foreign server (${FOREIGN_LIST//,/ and }) to authorize (copy the whole line):

id isaho-tunnel >/dev/null 2>&1 || useradd -r -m -s /usr/sbin/nologin isaho-tunnel; mkdir -p ~isaho-tunnel/.ssh && touch ~isaho-tunnel/.ssh/authorized_keys && sed -i '\\|$(cut -d' ' -f2 "$KEY.pub")|d' ~isaho-tunnel/.ssh/authorized_keys && echo 'restrict,port-forwarding,permitopen=\"127.0.0.1:443\",permitopen=\"127.0.0.1:2097\" $PUB' >> ~isaho-tunnel/.ssh/authorized_keys && chown -R isaho-tunnel: ~isaho-tunnel/.ssh && chmod 700 ~isaho-tunnel/.ssh && chmod 600 ~isaho-tunnel/.ssh/authorized_keys && echo AUTHORIZED

Check tunnels here:  systemctl --no-pager -l status 'isaho-tunnel@*' | grep -E 'tunnel #|Active'
"
    exit 0
fi

FOREIGN_IP=${1:?usage: bash relay.sh <foreign-server-ip> [listen-port] [foreign-port]}
LISTEN_PORT=${2:-443}
FOREIGN_PORT=${3:-443}

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

echo "==> Checking the link to $FOREIGN_IP:$FOREIGN_PORT"
if timeout 8 bash -c "exec 3<>/dev/tcp/$FOREIGN_IP/$FOREIGN_PORT" 2>/dev/null; then
    echo "    TCP OK"
else
    echo "✖ This server cannot open TCP to $FOREIGN_IP:$FOREIGN_PORT. Ask the provider about foreign traffic."
    exit 1
fi

if ss -Htln "sport = :$LISTEN_PORT" | grep -q .; then
    echo "✖ Port $LISTEN_PORT is already used on this server:"; ss -Htlnp "sport = :$LISTEN_PORT"; exit 1
fi

command -v nft >/dev/null || { apt-get update -qq && apt-get install -y -qq nftables; }

cat >/etc/isaho-relay.nft <<NFT
table ip isaho_relay
delete table ip isaho_relay
table ip isaho_relay {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        tcp dport $LISTEN_PORT dnat to $FOREIGN_IP:$FOREIGN_PORT
    }
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip daddr $FOREIGN_IP tcp dport $FOREIGN_PORT masquerade
    }
    chain forward {
        type filter hook forward priority mangle; policy accept;
        tcp flags syn tcp option maxseg size set rt mtu
    }
}
NFT

cat >/etc/sysctl.d/99-isaho-relay.conf <<SYSCTL
net.ipv4.ip_forward=1
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.netfilter.nf_conntrack_max=262144
SYSCTL
sysctl --system >/dev/null 2>&1 || true

cat >/etc/systemd/system/isaho-relay.service <<UNIT
[Unit]
Description=IsaHo relay (NAT forward to $FOREIGN_IP:$FOREIGN_PORT)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/isaho-relay.nft
ExecStop=/usr/sbin/nft delete table ip isaho_relay

[Install]
WantedBy=multi-user.target
UNIT

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$LISTEN_PORT/tcp" >/dev/null
    ufw route allow proto tcp to "$FOREIGN_IP" port "$FOREIGN_PORT" >/dev/null
fi

systemctl daemon-reload
systemctl enable -q isaho-relay
systemctl restart isaho-relay

MY_IP=$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
echo "
✅ Relay is up: $MY_IP:$LISTEN_PORT -> $FOREIGN_IP:$FOREIGN_PORT

Now in the Telegram bot: ⚙️ تنظیمات -> 🇮🇷 سرور واسط -> send:  $MY_IP:$LISTEN_PORT
Remove with: systemctl disable --now isaho-relay
"
