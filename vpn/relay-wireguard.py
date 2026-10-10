#!/usr/bin/env python3
"""Manage HAProxy for provisioned WireGuard or verified-TLS Backhaul paths."""
import argparse
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time

CONFIG = Path("/etc/isaho-relay.conf")
PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def read_config(path=CONFIG):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def endpoint(value, loopback=False):
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("expected a private IPv4 address and TCP port")
    address = ipaddress.IPv4Address(host)
    if not (address.is_loopback if loopback else any(address in network for network in PRIVATE_NETS)):
        raise ValueError("WireGuard destinations must be private IPv4 addresses")
    return str(address), int(port)


def settings(config):
    mode = config.get("TRANSPORT")
    if mode not in {"wireguard", "backhaul"}:
        raise ValueError("unsupported managed transport")
    prefix = "WG" if mode == "wireguard" else "BH"
    interfaces = config.get("WG_INTERFACE" if mode == "wireguard" else "BH_SERVICES", "").split(",")
    pattern = r"[a-zA-Z0-9_.-]{1,15}" if mode == "wireguard" else r"isaho-backhaul-[a-z0-9-]+"
    if not interfaces or any(not re.fullmatch(pattern, name) for name in interfaces):
        raise ValueError("invalid managed transport identity")
    data = [endpoint(value, mode == "backhaul") for value in config.get(prefix + "_DATA_ENDPOINTS", "").split(",")]
    if len(data) > 8 or len(set(data)) != len(data):
        raise ValueError("provide one to eight distinct data destinations")
    control = endpoint(config.get(prefix + "_CONTROL_ENDPOINT", ""), mode == "backhaul")
    public = ipaddress.IPv4Address(config.get("PUBLIC_IP", ""))
    if public.is_unspecified or public.is_loopback:
        raise ValueError("PUBLIC_IP must identify the relay")
    return interfaces, data, control


def render(config):
    _, data, control = settings(config)
    rows = ["global", "    log /dev/log local0 warning", "    maxconn 50000", "",
            "defaults", "    mode tcp", "    log global", "    timeout connect 5s",
            "    timeout client 2h", "    timeout server 2h", "    option tcpka", "",
            "frontend vpn", "    bind :443", "    default_backend tunnels", "",
            "backend tunnels", "    balance leastconn", "    option ssl-hello-chk"]
    # Node Reality sockets do not accept PROXY headers. Existing connections are allowed to
    # drain on transient health failures instead of terminating every user's session.
    rows += [f"    server wg_data_{index} {host}:{port} check inter 5s fall 3 rise 2"
             for index, (host, port) in enumerate(data, 1)]
    rows += ["", "frontend sub", "    bind :2096", "    default_backend sub", "",
             "backend sub", "    option httpchk GET /", "    http-check expect status 404",
             f"    server wg_control {control[0]}:{control[1]} check inter 10s fall 3 rise 2", ""]
    return "\n".join(rows)


def check_routes(config):
    interfaces, data, control = settings(config)
    for host, port in [*data, control]:
        result = subprocess.run(["ip", "-j", "route", "get", host], capture_output=True,
                                text=True, check=True, timeout=5)
        routes = json.loads(result.stdout)
        expected = interfaces if config["TRANSPORT"] == "wireguard" else ["lo"]
        if not routes or routes[0].get("dev") not in expected:
            raise ValueError("private endpoint is not routed through the configured WireGuard interface")
        with socket.create_connection((host, port), timeout=3):
            pass


def status(config, now=None):
    interfaces, data, control = settings(config)
    now = time.time() if now is None else now
    total = up = 0
    for interface in interfaces:
        try:
            if config["TRANSPORT"] == "backhaul":
                total += 1
                result = subprocess.run(["systemctl", "is-active", "--quiet", interface], timeout=3)
                up += int(result.returncode == 0)
                continue
            result = subprocess.run(["wg", "show", interface, "latest-handshakes"],
                                    capture_output=True, text=True, check=True, timeout=3)
            stamps = [int(line.split()[1]) for line in result.stdout.splitlines()]
            total += max(1, len(stamps))
            up += sum(1 for stamp in stamps if stamp > 0 and 0 <= now - stamp <= 180)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            total += 1
    total = max(1, total)

    def reachable(target):
        try:
            with socket.create_connection(target, timeout=1):
                return True
        except OSError:
            return False

    data_up = sum(reachable(target) for target in data)
    connection = None
    try:
        connection = http.client.HTTPConnection(*control, timeout=3)
        connection.request('GET', '/')
        response = connection.getresponse()
        control_up = response.status == 404
        response.read()
    except (OSError, http.client.HTTPException):
        control_up = False
    finally:
        if connection:
            connection.close()
    return {"transport": config["TRANSPORT"], "tunnels_total": total, "tunnels_up": up,
            "node_tunnels_total": 0, "node_tunnels_up": 0,
            "transport_healthy": up == total and data_up == len(data) and control_up,
            "data_paths_up": data_up, "data_paths_total": len(data), "control_up": control_up}


def set_version(config_path, version):
    if not re.fullmatch(r"[0-9a-f]{7,40}|main|manual", version):
        raise ValueError("invalid version")
    # Preserve endpoints, identities and comments; peer private files are never opened.
    path = Path(config_path)
    lines = [line for line in path.read_text().splitlines() if not line.startswith("VERSION=")]
    lines.append("VERSION=" + version)
    fd, temp = tempfile.mkstemp(prefix=".isaho-relay-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "status", "set-version"))
    parser.add_argument("version", nargs="?")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    config = read_config(args.config)
    settings(config)
    if args.command == "render":
        if args.check:
            check_routes(config)
        print(render(config), end="")
    elif args.command == "status":
        print(json.dumps(status(config)))
    else:
        set_version(args.config, args.version or "")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"WireGuard relay validation failed: {type(error).__name__}") from None
