#!/usr/bin/env bash
# IsaHo VPN installer: Xray (VLESS Reality + VLESS XHTTP over Cloudflare CDN) + Telegram bot.
# Re-running it upgrades the bot and keeps existing keys/users.
set -euo pipefail

APP_DIR=/opt/isaho-vpn
CONF_DIR=/etc/isaho-vpn
DATA_DIR=/var/lib/isaho-vpn
ENV_FILE=$CONF_DIR/vpn.env
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

green() { echo -e "\e[32m$*\e[0m"; }
yellow() { echo -e "\e[33m$*\e[0m"; }
red() { echo -e "\e[31m$*\e[0m"; }
die() { red "✖ $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo bash install.sh)"
command -v apt-get >/dev/null || die "Only Debian/Ubuntu are supported"
[[ -f $SRC_DIR/bot/main.py ]] || die "bot/ directory not found next to install.sh"

ask() { # ask VAR "prompt" "default"
    local var=$1 prompt=$2 def=${3:-} val
    if [[ -n ${!var:-} ]]; then return; fi
    read -rp "$prompt${def:+ [$def]}: " val </dev/tty
    printf -v "$var" '%s' "${val:-$def}"
}

# Existing install: reuse its values (keys, ports, paths)
if [[ -f $ENV_FILE ]]; then
    yellow "Existing install found, upgrading (keys and users are kept)."
    set -a; source "$ENV_FILE"; set +a
fi

green "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl unzip openssl python3 python3-venv python3-pip ca-certificates iproute2 >/dev/null

DETECTED_IP=$(curl -4 -fsS --max-time 10 https://api.ipify.org || curl -4 -fsS --max-time 10 https://ifconfig.me || true)

ask BOT_TOKEN   "Telegram bot token (from @BotFather)"
ask ADMIN_IDS   "Admin Telegram numeric ID(s), comma separated"
ask SERVER_IP   "Server public IP" "$DETECTED_IP"
ask DOMAIN      "Domain/subdomain proxied by Cloudflare (orange cloud), e.g. cdn.example.com"
ask REALITY_SNI "Reality camouflage site (must support TLS1.3/H2)" "www.speedtest.net"
ask BRAND       "Brand name shown in configs" "IsaHo"
[[ -n $BOT_TOKEN && -n $ADMIN_IDS && -n $SERVER_IP && -n $DOMAIN ]] || die "Missing required values"

for p in 443 2053 2096; do
    owner=$(ss -Htlnp "sport = :$p" | head -1)
    if [[ -n $owner && $owner != *xray* && $owner != *python* ]]; then
        die "Port $p is used by another program: $owner"
    fi
done

if ! timeout 8 openssl s_client -connect "$REALITY_SNI:443" -servername "$REALITY_SNI" -tls1_3 -alpn h2 </dev/null 2>/dev/null | grep -q "ALPN protocol: h2"; then
    yellow "⚠ $REALITY_SNI did not answer with TLS1.3 + h2 from this server; pick another camouflage site if Reality fails."
fi

green "==> Installing Xray-core"
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install -u root >/dev/null
XRAY_BIN=/usr/local/bin/xray
$XRAY_BIN version | head -1

mkdir -p "$CONF_DIR" "$DATA_DIR" "$APP_DIR"
chmod 700 "$CONF_DIR" "$DATA_DIR"

if [[ -z ${REALITY_PRIVATE_KEY:-} ]]; then
    KEYS=$($XRAY_BIN x25519)
    REALITY_PRIVATE_KEY=$(awk -F': *' 'tolower($1) ~ /private/ {print $2; exit}' <<<"$KEYS")
    REALITY_PUBLIC_KEY=$(awk -F': *' 'tolower($1) ~ /public|password/ {print $2; exit}' <<<"$KEYS")
    [[ -n $REALITY_PRIVATE_KEY && -n $REALITY_PUBLIC_KEY ]] || die "Could not parse 'xray x25519' output: $KEYS"
fi
REALITY_SHORT_ID=${REALITY_SHORT_ID:-$(openssl rand -hex 8)}
CDN_PATH=${CDN_PATH:-/$(openssl rand -hex 10)}

if [[ ! -f $CONF_DIR/cert.pem ]]; then
    green "==> Creating origin certificate (Cloudflare SSL mode: Full)"
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
        -keyout "$CONF_DIR/key.pem" -out "$CONF_DIR/cert.pem" -subj "/CN=$DOMAIN" \
        -addext "subjectAltName=DNS:$DOMAIN" 2>/dev/null
    chmod 600 "$CONF_DIR/key.pem"
fi

cat >"$ENV_FILE" <<ENV
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
BRAND=$BRAND
SERVER_IP=$SERVER_IP
DOMAIN=$DOMAIN
REALITY_PORT=443
REALITY_SNI=$REALITY_SNI
REALITY_DEST=$REALITY_SNI:443
REALITY_PRIVATE_KEY=$REALITY_PRIVATE_KEY
REALITY_PUBLIC_KEY=$REALITY_PUBLIC_KEY
REALITY_SHORT_ID=$REALITY_SHORT_ID
CDN_PORT=2053
CDN_PATH=$CDN_PATH
SUB_PORT=2096
CERT_FILE=$CONF_DIR/cert.pem
KEY_FILE=$CONF_DIR/key.pem
DATA_DIR=$DATA_DIR
XRAY_BIN=$XRAY_BIN
XRAY_CONFIG=/usr/local/etc/xray/config.json
XRAY_API=127.0.0.1:10085
ENV
chmod 600 "$ENV_FILE"

green "==> Installing bot"
rm -rf "$APP_DIR/bot"
cp -r "$SRC_DIR/bot" "$APP_DIR/bot"
cp "$SRC_DIR/install.sh" "$APP_DIR/install.sh"
[[ -d $APP_DIR/venv ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/bot/requirements.txt"

cat >/etc/systemd/system/isaho-bot.service <<UNIT
[Unit]
Description=IsaHo VPN Telegram bot
After=network-online.target xray.service
Wants=network-online.target

[Service]
WorkingDirectory=$APP_DIR/bot
ExecStart=$APP_DIR/venv/bin/python main.py
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
UNIT

mkdir -p /etc/systemd/system/xray.service.d
cat >/etc/systemd/system/xray.service.d/isaho.conf <<UNIT
[Service]
LimitNOFILE=1048576
UNIT

green "==> Network tuning (BBR, buffers)"
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
net.ipv4.tcp_notsent_lowat=16384
net.core.somaxconn=8192
net.ipv4.tcp_max_syn_backlog=8192
net.ipv4.ip_local_port_range=10000 65000
fs.file-max=1048576
SYSCTL
sysctl --system >/dev/null 2>&1 || true

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    green "==> Opening firewall ports"
    for p in 443 2053 2096; do ufw allow "$p/tcp" >/dev/null; done
fi

cat >/usr/local/bin/isaho <<'CLI'
#!/usr/bin/env bash
case "${1:-}" in
  status)  systemctl --no-pager status xray isaho-bot ;;
  logs)    journalctl -u isaho-bot -u xray -n 100 -f ;;
  restart) systemctl restart xray isaho-bot && echo restarted ;;
  update)  bash /opt/isaho-vpn/install.sh ;;
  env)     ${EDITOR:-nano} /etc/isaho-vpn/vpn.env && systemctl restart isaho-bot ;;
  uninstall)
    read -rp "Remove bot, users DB and Xray? [y/N] " a; [[ $a == y ]] || exit
    systemctl disable --now isaho-bot xray
    rm -rf /opt/isaho-vpn /var/lib/isaho-vpn /etc/isaho-vpn /etc/systemd/system/isaho-bot.service /usr/local/bin/isaho
    bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ remove --purge ;;
  *) echo "usage: isaho {status|logs|restart|update|env|uninstall}" ;;
esac
CLI
chmod +x /usr/local/bin/isaho

systemctl daemon-reload
systemctl enable -q xray isaho-bot
systemctl restart isaho-bot
sleep 5
systemctl is-active -q isaho-bot || { journalctl -u isaho-bot -n 30 --no-pager; die "bot failed to start"; }
systemctl is-active -q xray || { journalctl -u xray -n 30 --no-pager; die "xray failed to start"; }

green "
✅ Installed. Open your bot in Telegram and send /start

Cloudflare checklist for $DOMAIN:
  • DNS: A record $DOMAIN -> $SERVER_IP with Proxy ON (orange cloud)
  • SSL/TLS -> Overview: mode \"Full\"  (not Flexible, not Full strict)
  • Network: gRPC ON, WebSockets ON
  • Speed -> Optimization: turn OFF Rocket Loader for this host

Manage from the shell with:  isaho status | logs | restart | update | env | uninstall
"
