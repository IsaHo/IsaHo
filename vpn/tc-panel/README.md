# TC VPN — full-feature upstream panel, Telegram bridge

## Pinned upstream

3X-UI **v3.10.2**, commit `608ef4e4010bf83e2e78328e08f77efc051ad4d7`.
Upstream: https://github.com/MHSanaei/3x-ui . Retain upstream LICENSE and
attribution in every distribution. TC VPN is a branded derivative, not a rewrite.

`brand.py SOURCE` applies a small, fail-closed display-only overlay. Login,
sidebar, page titles, command palette, API-docs heading and PWA name become
TC VPN. API endpoints, backend implementation, authentication, upstream support
links, all features, and database identity remain untouched. Internal `x-ui`
service/file names and icon filenames intentionally remain compatible.

**Never rename `internal/config/name`: it selects the database filename.**
Do not replace all upstream strings or remove license notices. Automatic upstream
updates can replace branding; rebuild the overlay against a reviewed release.

## Build

```sh
git clone https://github.com/MHSanaei/3x-ui.git panel-source
git -C panel-source checkout --detach 608ef4e4010bf83e2e78328e08f77efc051ad4d7
python3 vpn/tc-panel/brand.py panel-source
cd panel-source/frontend
npm ci --ignore-scripts
npm run build
cd ..
# Native Linux arm64 build (or use an appropriate C cross-compiler):
CGO_ENABLED=1 GOOS=linux GOARCH=arm64 go build -trimpath -o tc-vpn .
```

Build architecture must match the server. Back up the complete panel installation,
consistent SQLite database, configuration and certificates before deployment.
Rebranding requires a panel restart, which can interrupt its Xray; obtain an
approved maintenance window. Preserve the old binary and database for rollback.
Do not run the custom binary against a production DB merely to smoke-test it.

## Telegram phase one — explicitly read-only

`bot/tcpanel.py` adds an admin-only **TC VPN · پنل وب** status action under
Management. It uses only `GET /panel/api/server/status` with an upstream **monitor**
scope token. It does not migrate accounts, generate customer links, reset usage,
change inbound settings, or restart anything. Main bot deployment remains the
usual guarded bot-only update.

Enroll `/etc/isaho-vpn/tc-panel.json` as root-owned mode 0600 (never commit):

```json
{"url":"https://PANEL_HOST:2053/PRIVATE_BASE_PATH/","token":"MONITOR_TOKEN","sha256":"64_HEX_LEAF_CERT_FINGERPRINT"}
```

Obtain the leaf-certificate SHA256 fingerprint over authenticated SSH, not from
an unauthenticated network scan. The bridge verifies the pin before sending the
token. A rotated certificate fails closed until re-enrolled. The bearer credential
does not appear in chat, logs, callback data, or the web-panel URL.

Next phase must opt in test accounts before production synchronization. Define a
single authority for lifecycle and traffic; do not enable both old node-config
writer and panel writer, reset existing counters, or migrate all users implicitly.
