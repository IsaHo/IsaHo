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
    OVERRIDE=${3:-}
    D=/tmp/isaho-test; mkdir -p "$D"
    if [[ ! -x $D/xray ]]; then
        command -v unzip >/dev/null || apt-get install -y -qq unzip >/dev/null
        curl -fsSL -o "$D/x.zip" https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip
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
