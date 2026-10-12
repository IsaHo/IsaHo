# Traffic overhead controls

Speed selection samples one healthy path every five minutes, using 512 KiB per
sample. The per-relay UTC daily payload reservation is capped at 128 MiB, including
failed attempts. This is not a hard NIC-byte cap: TLS, tunnel headers and retransmits
are additional. Exhaustion/cooldown preserves existing weights and sessions;
HAProxy's independent availability checks continue. Scores expire after three
hours to support a complete rotation across up to eight paths.

Node backup requests explicitly negotiate gzip. Older agents get plain JSON;
upgraded agents accept either format. The backup schedule, database snapshot,
certificate pinning and takeover files are unchanged.

An optional `fallback_guard: true` boolean on one node applies REALITY's native
`limitFallbackUpload` and `limitFallbackDownload` fields. They limit unauthenticated
fallback connections only, not valid VLESS users. Main-server inbounds are untouched.
Stable, per-node parameters vary between 64–128 KiB unmetered, 2–4 KiB/s sustained
and 8–16 KiB/s burst. Limits are per connection, not a global traffic budget.

REALITY upstream warns that fallback rate limits may introduce detectable patterns.
This is an opt-in cost mitigation, not a promise against filtering. Validate with
the installed Xray binary and pilot one node before rolling deployment. Drain new
selections to other proven paths, restart only the selected node, verify authenticated
connections, then restore it. Never change user keys or publish new subscriptions
for this rollout. Disabling the flag restores the original generated configuration;
the selected node must reapply it. Keep local config backups for failed rollbacks.

Compare NIC deltas and authenticated user counters over matching periods. Do not
compare RX+TX to customer usage, or since-boot counters to lifetime provider billing.
Persistent connections to the REALITY cover destination suggest fallback activity,
but a short snapshot does not establish the entire historical billing discrepancy.
