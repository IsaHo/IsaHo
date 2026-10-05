#!/usr/bin/env bash
# Basic hardening for the VPN servers (foreign and Iranian relay):
#  - fail2ban bans IPs that keep failing SSH logins
#  - password logins are turned off ONLY if root already has an SSH key, so you can't lock yourself out
#   usage: bash harden.sh [trusted-ip ...]     (e.g. the other server's IP)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

echo "==> fail2ban"
command -v fail2ban-server >/dev/null || { apt-get update -qq && apt-get install -y -qq fail2ban >/dev/null; }
cat >/etc/fail2ban/jail.d/isaho.local <<JAIL
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 $*
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
JAIL
systemctl enable -q fail2ban
systemctl restart fail2ban
echo "    fail2ban active (5 failed logins in 10 min -> 1h ban)"

echo "==> SSH login"
if grep -qsvE 'isaho-relay|^\s*$|^#' /root/.ssh/authorized_keys; then
    cat >/etc/ssh/sshd_config.d/99-isaho.conf <<SSHD
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
SSHD
    sshd -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd)
    echo "    password login disabled; root can log in with its SSH key only"
else
    echo "    ⚠ root has no SSH key yet, so password login stays ON (to avoid locking you out)."
    echo "      In Termius: Keychain -> Generate key -> 'Export to host' this server, then run this script again."
fi
echo "✅ done"
