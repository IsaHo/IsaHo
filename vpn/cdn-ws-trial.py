#!/usr/bin/env python3
"""Isolated, Cloudflare-only WebSocket origin trial. Never edit customer Xray."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import uuid

ROOT = Path('/etc/isaho-cdn-ws-trial')
UNITS = Path('/etc/systemd/system')
CORE = 'isaho-cdn-ws-trial'
SOCKET = 'isaho-cdn-ws-trial-edge'
FIREWALL = 'isaho-cdn-ws-trial-fw'
TABLE = 'isaho_cdn_ws_trial'
PRIVATE_PORT = 2054
PUBLIC_PORT = 8444
HOSTNAME = 'cdn-test.zkim.app'
TRIAL_PATH = '/isaho-ws-trial-20261016'


def run(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=25)


def ranges(text):
    parsed = [ipaddress.IPv4Network(value) for value in text.split()]
    if any(network.prefixlen < 8 for network in parsed):
        raise ValueError('overbroad address range')
    values = [str(network) for network in parsed]
    if not 10 <= len(values) <= 100:
        raise ValueError('unexpected Cloudflare range count')
    return values


def public_address(value):
    address = ipaddress.IPv4Address(value)
    if not address.is_global:
        raise ValueError('a public, single-interface IPv4 address is required')
    return str(address)


def firewall(public_ip, networks):
    address = public_address(public_ip)
    networks = ranges('\n'.join(networks))
    return (f'table inet {TABLE} {{\n chain input {{\n'
            '  type filter hook input priority -10; policy accept;\n'
            f'  ip daddr {address} tcp dport {PUBLIC_PORT} ip saddr {{ '
            + ', '.join(networks) + ' } accept\n'
            f'  ip daddr {address} tcp dport {PUBLIC_PORT} drop\n }}\n}}\n')


def configuration():
    return {
        'log': {'loglevel': 'none'},
        'inbounds': [{
            'tag': 'cdn-ws-isolated-trial', 'listen': '127.0.0.1',
            'port': PRIVATE_PORT, 'protocol': 'vless',
            'settings': {'clients': [{'id': str(uuid.uuid4()), 'email': '__isolated_ws_trial__'}],
                         'decryption': 'none'},
            'streamSettings': {
                'network': 'ws', 'security': 'tls',
                'wsSettings': {'path': TRIAL_PATH},
                'tlsSettings': {'alpn': ['http/1.1'], 'certificates': [{
                    'certificateFile': '/etc/isaho-node/cert.pem',
                    'keyFile': '/etc/isaho-node/key.pem'}]},
            },
            'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True},
        }],
        'outbounds': [{'tag': 'direct', 'protocol': 'freedom'},
                      {'tag': 'block', 'protocol': 'blackhole'}],
        'routing': {'rules': [
            {'type': 'field', 'ip': ['127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12',
                                    '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10',
                                    '::1/128', 'fc00::/7', 'fe80::/10'], 'outboundTag': 'block'},
            {'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': 'block'},
        ]},
    }


def units(public_ip, proxy):
    address = public_address(public_ip)
    if proxy not in ('/usr/lib/systemd/systemd-socket-proxyd',
                     '/lib/systemd/systemd-socket-proxyd'):
        raise ValueError('unexpected socket proxy binary')
    hardened = ('NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\n'
                'ProtectHome=true\nLimitNOFILE=4096\nTimeoutStopSec=10\n')
    return {
        CORE + '.service': (
            '[Unit]\nDescription=IsaHo isolated WebSocket trial; no customer accounts\n'
            '[Service]\nType=simple\nExecStart=/usr/local/bin/xray run -c '
            + str(ROOT / 'config.json') + '\nRestart=no\n' + hardened),
        FIREWALL + '.service': (
            '[Unit]\nDescription=Cloudflare-only firewall for isolated trial port\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
            'ExecStart=/usr/sbin/nft -f ' + str(ROOT / 'cloudflare-only.nft') + '\n'
            f'ExecStop=/usr/sbin/nft delete table inet {TABLE}\n'),
        SOCKET + '.socket': (
            f'[Unit]\nDescription=Isolated CDN trial edge\nRequires={FIREWALL}.service {CORE}.service\n'
            f'After={FIREWALL}.service {CORE}.service\n[Socket]\n'
            f'ListenStream={address}:{PUBLIC_PORT}\nNoDelay=true\nBacklog=128\n'),
        SOCKET + '.service': (
            f'[Unit]\nDescription=Isolated CDN trial TLS passthrough\nRequires={SOCKET}.socket\n'
            f'After={SOCKET}.socket\n[Service]\nExecStart={proxy} --connections-max=128 '
            f'127.0.0.1:{PRIVATE_PORT}\n' + hardened),
    }


def stop():
    # Only these dedicated units/table are in scope; leave files for audit/recovery.
    run('systemctl', 'stop', SOCKET + '.socket', check=False)
    run('systemctl', 'stop', SOCKET + '.service', check=False)
    run('systemctl', 'stop', CORE + '.service', check=False)
    run('systemctl', 'stop', FIREWALL + '.service', check=False)
    remaining = {name: run('systemctl', 'is-active', name, check=False).stdout.strip()
                 for name in (SOCKET + '.socket', CORE + '.service', FIREWALL + '.service')}
    if any(value in ('active', 'activating', 'deactivating') for value in remaining.values()):
        raise RuntimeError('trial did not stop cleanly')
    return {'trial_stopped': True, 'files_retained': str(ROOT), 'customer_services_touched': False}


def install(public_ip):
    address = public_address(public_ip)
    proxy = next((p for p in ('/usr/lib/systemd/systemd-socket-proxyd',
                             '/lib/systemd/systemd-socket-proxyd') if Path(p).is_file()), None)
    if not proxy or not shutil.which('nft'):
        raise RuntimeError('required existing system tools missing')
    files = units(address, proxy)
    if ROOT.exists() or any((UNITS / name).exists() for name in files):
        raise RuntimeError('trial already exists; refusing overwrite')
    if run('nft', 'list', 'table', 'inet', TABLE, check=False).returncode == 0:
        raise RuntimeError('trial firewall table already exists')
    for port in (PRIVATE_PORT, PUBLIC_PORT):
        if run('ss', '-Hltn', 'sport = :' + str(port)).stdout.strip():
            raise RuntimeError('trial port already occupied')
    before = run('systemctl', 'show', 'xray', '-p', 'MainPID', '--value').stdout.strip()
    if not before.isdigit() or int(before) == 0:
        raise RuntimeError('customer Xray not running')
    with urllib.request.urlopen('https://www.cloudflare.com/ips-v4', timeout=15) as response:
        networks = ranges(response.read(65536).decode())
    ROOT.mkdir(mode=0o700)
    (ROOT / 'customer-xray-pid-before').write_text(before)
    created = []
    try:
        config = ROOT / 'config.json'
        config.write_text(json.dumps(configuration()))
        config.chmod(0o600)
        rules = ROOT / 'cloudflare-only.nft'
        rules.write_text(firewall(address, networks))
        rules.chmod(0o600)
        run('/usr/local/bin/xray', 'run', '-test', '-c', str(config))
        run('nft', '-c', '-f', str(rules))
        for name, content in files.items():
            file = UNITS / name
            with file.open('x') as stream:
                stream.write(content)
            file.chmod(0o644)
            created.append(file)
        run('systemctl', 'daemon-reload')
        # Do not enable at boot: a diagnostic must not become a second production stack.
        run('systemctl', 'start', CORE + '.service', FIREWALL + '.service', SOCKET + '.socket')
        if run('systemctl', 'show', 'xray', '-p', 'MainPID', '--value').stdout.strip() != before:
            raise RuntimeError('customer Xray PID changed during install')
        return {'trial_started': True, 'hostname': HOSTNAME,
                'public_port': PUBLIC_PORT, 'private_port': PRIVATE_PORT,
                'new_test_identity_only': True, 'customer_xray_pid_unchanged': True,
                'enabled_at_boot': False}
    except Exception:
        if created:
            stop()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'stop'])
    parser.add_argument('--public-ip')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('root required')
    if args.action == 'install' and not args.public_ip:
        parser.error('--public-ip required')
    try:
        print(json.dumps(install(args.public_ip) if args.action == 'install' else stop()))
    except Exception as error:
        # Config validation/journals may contain secrets. Never echo exception text.
        print(json.dumps({'error': type(error).__name__, 'private_error_withheld': True}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
