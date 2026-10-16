# Isolated WebSocket CDN diagnostic

This is an opt-in diagnostic, **not** a customer backend or panel migration.
Run only with authorization for the selected node and test hostname.

Prerequisites: existing node Xray binary/certificate, nftables, systemd socket
proxy, free loopback 2054 and public IPv4 8444. No packages are installed. The
certificate must cover `cdn-test.zkim.app` and remain valid at the origin.

```
python3 cdn-ws-trial.py install --public-ip NODE_IPV4
python3 cdn-ws-trial.py stop
```

Installation refuses existing trial files, unit names, firewall tables and occupied
ports. It creates one new isolated test identity (never imports customer accounts)
in a root-only config. A separate Xray instance binds only loopback 2054, using
WebSocket with TLS and HTTP/1.1. A dedicated socket proxy exposes IPv4 8444 only
to the current official Cloudflare IPv4 ranges, via its own narrow nftables table.
SSH, existing VPN listeners, the production Xray service/config and node timers
are not changed. Units are deliberately not enabled at boot.

Only after local CA-verified origin tests succeed, change **only** the test DNS
record to this node and the test hostname's Origin Rule to 8444. Back up and verify
the exact old values before writing. Existing hostnames, rules and subscriptions
must remain unchanged. Do not change a live listener to route this trial.

Verify from both Iranian relays, explicitly separating IPv4 and IPv6. Require a
complete authenticated request through the VPN; HTTP 101 alone is insufficient.
Validate the public edge certificate normally. For a local direct-origin test,
trust the official Cloudflare Origin CA; never disable certificate verification.
Do not print the test UUID, config JSON or private keys in diagnostics.

If tests fail, first restore only the test DNS/Origin Rule after checking no other
actor changed them, then stop the dedicated trial units. `stop` retains config and
unit files for audit/recovery; it does not erase accounts or alter production.
The isolated identity is not a purchased account: no selling, publishing to
customer subscriptions, or claiming shared billing before a separate integration.
