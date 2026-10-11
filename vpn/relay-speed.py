#!/usr/bin/env python3
"""Bounded, rotating end-to-end samples; change HAProxy weights, never sessions."""
import argparse
import csv
import fcntl
import io
import json
import os
from pathlib import Path
import re
import socket
import statistics
import subprocess
import tempfile
import time

BYTES = 2 * 1024 * 1024
URL = f'https://speed.cloudflare.com/__down?bytes={BYTES}'
TTL = 3600


def runtime(path, command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(path)
        sock.sendall((command + '\n').encode())
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                return b''.join(chunks).decode()
            chunks.append(data)


def validate(config):
    paths = config['paths']
    if not 1 <= len(paths) <= 8 or len({p['name'] for p in paths}) != len(paths):
        raise ValueError('invalid path list')
    for path in paths:
        if not re.fullmatch(r'wg_data_[1-8]', path['name']):
            raise ValueError('unmanaged server')
        if type(path['port']) is not int or not 1024 <= path['port'] <= 65535:
            raise ValueError('invalid loopback port')
    if config.get('socket') != '/run/isaho-speed.sock':
        raise ValueError('unexpected runtime socket')
    if config['outbound']['protocol'] != 'vless':
        raise ValueError('authenticated VLESS probe required')
    return paths


def scores(state, paths, now):
    result = {}
    for path in paths:
        samples = state.get('samples', {}).get(path['name'], [])
        fresh = [s for s in samples if 0 <= now - s['time'] <= TTL and s.get('ok')]
        # One success is insufficient to route customer traffic to a previously untested path.
        if len(fresh) >= 2 and samples and samples[-1].get('ok'):
            result[path['name']] = statistics.median(s['mbps'] for s in fresh[-3:])
    return result


def choose(state, measured, available, now):
    values = {k: v for k, v in measured.items() if k in available}
    current = state.get('preferred')
    if not values:
        return current, False
    best = max(values, key=values.get)
    if current in values and current != best:
        if now - state.get('switched_at', 0) < 600 or values[best] < values[current] * 1.25:
            best = current
    return best, best != current


def plan(preferred, measured, names):
    # Never zero/drain/disable a fallback: HAProxy can fail over without waiting for the timer.
    weights = {name: 1 for name in names}
    if preferred not in weights:
        return weights
    weights[preferred] = 200
    base = measured.get(preferred, 0)
    for name, value in measured.items():
        if name in weights and base > 0 and value >= base * .85:
            weights[name] = min(200, max(100, round(200 * value / base)))
    return weights


def probe(config, path):
    outbound = json.loads(json.dumps(config['outbound']))
    outbound['settings']['vnext'][0].update(address='127.0.0.1', port=path['port'])
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='probe-', dir=config['state_dir']) as directory:
        file = Path(directory) / 'config.json'
        file.write_text(json.dumps({'log': {'loglevel': 'none'}, 'inbounds': [
            {'listen': '127.0.0.1', 'port': port, 'protocol': 'socks'}], 'outbounds': [outbound]}))
        file.chmod(0o600)
        proc = subprocess.Popen([config['xray'], 'run', '-c', str(file)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1)
            response = subprocess.run(['curl', '-4', '-sS', '--connect-timeout', '4', '--max-time', '8',
                '--socks5-hostname', f'127.0.0.1:{port}', '-o', '/dev/null', '-w',
                '%{http_code} %{size_download} %{speed_download} %{time_starttransfer}', URL],
                capture_output=True, text=True, timeout=10,
                env={k: v for k, v in os.environ.items() if 'proxy' not in k.lower()})
            values = response.stdout.split()
            ok = response.returncode == 0 and len(values) == 4 and values[0] == '200' and int(values[1]) == BYTES
            return {'time': int(time.time()), 'ok': ok,
                    'mbps': round(float(values[2]) * 8 / 1e6, 2) if ok else 0,
                    'ttfb_ms': round(float(values[3]) * 1000) if ok else 0}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def save(file, state):
    tmp = file.with_suffix('.new')
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.chmod(0o600)
    os.replace(tmp, file)


def bootstrap(config, ready):
    """Observe until every candidate is proved; then one validated graceful reload."""
    if not config.get('bootstrap'):
        return True
    file = Path('/etc/isaho-relay.conf')
    old = file.read_text()
    if 'SPEED_AUTO=1' in old.splitlines():
        return True
    if not ready:
        return False
    helper = '/usr/local/lib/isaho/relay-wireguard.py'
    haproxy = Path('/etc/haproxy/haproxy.cfg')
    previous = haproxy.read_text()
    stamp = str(int(time.time()))
    directory = Path(config['state_dir'])
    for name, text in [('relay.before-auto-', old), ('haproxy.before-auto-', previous)]:
        backup = directory / (name + stamp)
        backup.write_text(text)
        backup.chmod(0o600)
    new = '\n'.join(line for line in old.splitlines() if not line.startswith('SPEED_AUTO=')) + '\nSPEED_AUTO=1\n'
    tmp = haproxy.with_suffix('.speed-new')
    try:
        file.write_text(new)
        file.chmod(0o600)
        rendered = subprocess.check_output(['python3', helper, 'render', '--check'], text=True, timeout=20)
        tmp.write_text(rendered)
        subprocess.run(['haproxy', '-c', '-f', str(tmp)], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if haproxy.read_text() != previous:
            raise RuntimeError('concurrent HAProxy edit')
        os.replace(tmp, haproxy)
        subprocess.run(['systemctl', 'reload', 'haproxy'], check=True, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        if file.read_text() == new:
            file.write_text(old)
            file.chmod(0o600)
        # Never overwrite another actor's config when validation detected a race.
        if 'rendered' in locals() and haproxy.read_text() == rendered:
            haproxy.write_text(previous)
        raise
    finally:
        if tmp.exists():
            tmp.unlink()
    return True


def run(config, apply=False, warmup=False):
    paths = validate(config)
    directory = Path(config['state_dir'])
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'skipped': 'already running'}
        file = directory / 'state.json'
        state = json.loads(file.read_text()) if file.exists() else {'samples': {}, 'preferred': paths[0]['name']}
        state.setdefault('applied_preferred', paths[0]['name'])
        # Allow the existing route to keep serving; never benchmark heavily on a busy relay.
        if os.getloadavg()[0] > 1.8 * (os.cpu_count() or 1):
            return {'skipped': 'CPU load', 'preferred': state.get('preferred')}
        rows = csv.DictReader(io.StringIO(runtime(config['socket'], 'show stat').lstrip('# ')))
        available = {row['svname'] for row in rows if row['pxname'] == 'tunnels' and row['status'].startswith('UP')}
        candidates = paths if warmup else [paths[int(state.get('cursor', 0)) % len(paths)]]
        for path in candidates:
            samples = state['samples'].setdefault(path['name'], [])
            if path['name'] in available:
                try:
                    samples.append(probe(config, path))
                except (OSError, ValueError, subprocess.SubprocessError):
                    samples.append({'time': int(time.time()), 'ok': False, 'mbps': 0})
            else:
                samples.append({'time': int(time.time()), 'ok': False, 'mbps': 0})
            state['samples'][path['name']] = samples[-3:]
        state['cursor'] = (int(state.get('cursor', 0)) + 1) % len(paths)
        now = int(time.time())
        measured = scores(state, paths, now)
        preferred, changed = choose({**state, 'preferred': state['applied_preferred']}, measured, available, now)
        state['preferred'] = preferred
        weights = plan(preferred, measured, [p['name'] for p in paths])
        activated = bool(apply and measured and bootstrap(config, set(measured) == {p['name'] for p in paths}))
        if activated:
            # Raise preferred paths before reducing others. No session shutdown commands.
            for name in sorted(weights, key=weights.get, reverse=True):
                result = runtime(config['socket'], f'set server tunnels/{name} weight {weights[name]}')
                if result.strip():
                    raise RuntimeError('HAProxy rejected weight update')
            state['applied_preferred'] = preferred
            if changed:
                state['switched_at'] = now
        state['updated_at'] = now
        state['weights'] = weights
        save(file, state)
        return {'preferred': preferred, 'mbps': measured, 'weights': weights,
                'applied': activated, 'sampled': [p['name'] for p in candidates],
                'bootstrap_pending': bool(apply and not activated)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/isaho-speed.json')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--warmup', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(run(json.loads(Path(args.config).read_text()), args.apply, args.warmup)))
    except Exception as error:
        # Config and transport errors can contain credentials: print only their class.
        print(json.dumps({'error': type(error).__name__, 'current_sessions': 'untouched'}))
        raise SystemExit(1)
