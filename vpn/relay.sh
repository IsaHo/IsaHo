#!/usr/bin/env bash
# Turns an Iranian VPS into a relay: TCP on LISTEN_PORT is forwarded (kernel NAT, no
# decryption) to the foreign server's Reality port. Users dial the Iranian IP; per-user
# accounting keeps working on the foreign server because the protocol is untouched.
#   usage: bash relay.sh <foreign-server-ip> [listen-port] [foreign-port]
set -euo pipefail

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
