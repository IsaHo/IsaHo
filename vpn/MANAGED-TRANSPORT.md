# Managed relay transport

`relay.sh wireguard` and `relay.sh backhaul` manage HAProxy and the existing report agent after secure peer provisioning. They validate endpoint routes and HAProxy before reloading. They never provision private peer keys, alter the default route, disable existing tunnels, or expose Xray listeners.

Keep `/etc/isaho-relay.conf` root-owned, mode 600. WireGuard uses `TRANSPORT=wireguard`, `WG_INTERFACE`, `WG_DATA_ENDPOINTS` and `WG_CONTROL_ENDPOINT`; destinations must be private IPv4 endpoints routed through the configured interface. Backhaul uses `TRANSPORT=backhaul`, `BH_SERVICES`, `BH_DATA_ENDPOINTS` and `BH_CONTROL_ENDPOINT`; destinations must be loopback endpoints served by the authenticated reverse transport. Both require `PUBLIC_IP` and preserve `VERSION` during updates.

Data destinations here are node Reality sockets without PROXY protocol. A primary-server Reality socket requiring PROXY protocol must not be added to this data list. Control destinations must bridge to the primary's localhost HTTP port 2097 so `/relay/report` retains its local-only authorization.

The bot update action preserves the configured transport. Restart reloads HAProxy instead of restarting WireGuard interfaces. Report health uses WireGuard handshakes or Backhaul service state plus endpoint connectivity; real VLESS probes remain the authority for customer-path health. Existing SSH relays retain their current behavior.

Backhaul v0.7.2 disables server-certificate verification in its built-in WSS client. The deployed path therefore uses its loopback WS client through stunnel with `verifyChain=yes` and `checkIP` against the pinned relay certificate; the relay serves WSS. Pin the release checksum, use separate random tokens for data/control, keep all local forward ports on loopback, and disable traffic sniffing and the monitoring web port. Never deploy the unverified built-in WSS client for control traffic.

Cut over only after actual VPN, subscription and report tests. Reload HAProxy, allow existing connections to drain, and disable only the replaced relay's SSH tunnel units. Retain previous configs and exact unit state for rollback.

Optional `BH_BACKUP_ENDPOINTS` (or `WG_BACKUP_ENDPOINTS`) names a subset of data endpoints to reserve for failover. At least one primary endpoint must remain. Existing configurations without this setting keep least-connection balancing across all endpoints. Prefer a measured fast primary with independent backup paths; test failover on a loopback-only staging listener, not by stopping a live customer tunnel.
