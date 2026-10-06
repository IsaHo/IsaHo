#!/usr/bin/env bash
# Finds which Xray transport survives the path Iran -> foreign server without SSH.
#   on the foreign server:  bash tunnel-test.sh server <iran-ip>
#   on the Iranian server:  bash tunnel-test.sh client <foreign-ip>
# Everything is temporary: the server side exits after 20 minutes, nothing is installed.
set -euo pipefail
ROLE=${1:?usage: tunnel-test.sh server <iran-ip> | client <foreign-ip>}
PEER=${2:?missing peer ip}
D=/tmp/xt; mkdir -p "$D"; cd "$D"
if [[ ! -x ./xray ]]; then
    if [[ -x /tmp/isaho-test/xray ]]; then cp /tmp/isaho-test/xray .; else
        command -v unzip >/dev/null || apt-get install -y -qq unzip >/dev/null
        curl -fsSL -o x.zip https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip
        unzip -oq x.zip
    fi
fi
pkill -f "^$D/xray run" 2>/dev/null || true; sleep 1

python3 - "$ROLE" "$PEER" >"$D/$ROLE.json" <<'PY'
import json, sys
role, peer = sys.argv[1], sys.argv[2]
UUID = "3f1c2d4e-8a7b-4c6d-9e0f-112233445566"
PRIV, PUB = "eNKIXkJeZozGRzIr1QedZr1yDqcC59-AFI__RFh-zWI", "VidDSzMdgl1KjAPk2QquC703est8oTR6fYZYEdfVx1M"
def enc(mode, server):
    return f"mlkem768x25519plus.{mode}.600s.{PRIV}" if server else f"mlkem768x25519plus.{mode}.0rtt.{PUB}"
HTTP_HDR = {"type": "http", "request": {"path": ["/"], "headers": {"Host": ["www.aparat.com"]}}}
# name, port, encryption mode (None = plain VLESS), client stream, server stream
T = [
    ("tcp-plain",        8090, None,     {"network": "raw"}, None),
    ("tcp-enc-native",   8091, "native", {"network": "raw"}, None),
    ("tcp-enc-random",   8092, "random", {"network": "raw"}, None),
    ("tcp-httphdr-enc",  8093, "random", {"network": "raw", "rawSettings": {"header": HTTP_HDR}}, None),
    ("ws-enc",           8094, "random", {"network": "ws", "wsSettings": {"path": "/ws", "host": "www.aparat.com"}}, None),
    ("xhttp-packet-enc", 8095, "random", {"network": "xhttp", "xhttpSettings": {"path": "/api", "host": "www.aparat.com", "mode": "packet-up"}},
                                         {"network": "xhttp", "xhttpSettings": {"path": "/api", "mode": "auto"}}),
    ("xhttp-stream-enc", 8096, "random", {"network": "xhttp", "xhttpSettings": {"path": "/api", "host": "www.aparat.com", "mode": "stream-one"}},
                                         {"network": "xhttp", "xhttpSettings": {"path": "/api", "mode": "auto"}}),
    ("grpc-enc",         8097, "random", {"network": "grpc", "grpcSettings": {"serviceName": "api"}}, None),
]
if role == "server":
    ib = []
    for name, port, mode, cs, ss in T:
        ss = ss or cs
        if cs.get("rawSettings"):  # server side of the http header disguise answers with a response
            ss = {"network": "raw", "rawSettings": {"header": {"type": "http"}}}
        ib.append({"tag": name, "port": port, "protocol": "vless",
                   "settings": {"clients": [{"id": UUID}], "decryption": enc(mode, True) if mode else "none"},
                   "streamSettings": ss})
    cfg = {"log": {"loglevel": "warning"}, "inbounds": ib, "outbounds": [{"protocol": "freedom"}]}
else:
    ib, ob, rules = [], [], []
    for i, (name, port, mode, cs, ss) in enumerate(T):
        ib.append({"tag": "in-" + name, "listen": "127.0.0.1", "port": 32000 + i, "protocol": "socks"})
        ob.append({"tag": name, "protocol": "vless",
                   "settings": {"vnext": [{"address": peer, "port": port,
                                           "users": [{"id": UUID, "encryption": enc(mode, False) if mode else "none"}]}]},
                   "streamSettings": cs})
        rules.append({"type": "field", "inboundTag": ["in-" + name], "outboundTag": name})
    cfg = {"log": {"loglevel": "warning"}, "inbounds": ib, "outbounds": ob, "routing": {"rules": rules}}
    print("\n".join(f"{32000+i} {t[0]}" for i, t in enumerate(T)), file=open("/tmp/xt/list", "w"))
print(json.dumps(cfg))
PY

if [[ $ROLE == server ]]; then
    iptables -C INPUT -p tcp -m multiport --dports 8090:8097 -s "$PEER" -j ACCEPT 2>/dev/null \
        || iptables -I INPUT -p tcp -m multiport --dports 8090:8097 -s "$PEER" -j ACCEPT
    (timeout 1200 "$D/xray" run -c "$D/server.json" >"$D/server.log" 2>&1 &)
    sleep 2
    pgrep -f "^$D/xray run" >/dev/null && echo "✅ READY — now run the client on the Iranian server (stops itself in 20 min)" \
        || { echo "✖ xray failed:"; tail -5 "$D/server.log"; }
    exit 0
fi

(timeout 900 "$D/xray" run -c "$D/client.json" >"$D/client.log" 2>&1 &)
sleep 2
pgrep -f "^$D/xray run" >/dev/null || { echo "✖ xray failed:"; tail -5 "$D/client.log"; exit 1; }
printf "%-18s %-16s %12s %12s\n" TRANSPORT EXIT-IP "HTTP Mbit" "HTTPS Mbit"
while read -r PORT NAME; do
    S="--socks5-hostname 127.0.0.1:$PORT"
    IP=$(curl -s -m 10 $S https://api.ipify.org || true)
    H=$(curl -s -o /dev/null -m 15 -w "%{speed_download}" $S http://speedtest.tele2.net/100MB.zip || true)
    T=$(curl -s -o /dev/null -m 15 -w "%{speed_download}" $S "https://speed.cloudflare.com/__down?bytes=100000000" || true)
    mb() { awk -v b="${1:-0}" 'BEGIN{printf "%.1f", b*8/1e6}'; }
    printf "%-18s %-16s %12s %12s\n" "$NAME" "${IP:-✖}" "$(mb "$H")" "$(mb "$T")"
done <"$D/list"
pkill -f "^$D/xray run" 2>/dev/null || true
