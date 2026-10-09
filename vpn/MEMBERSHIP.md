# Announcement-channel membership

The channel is configured independently from billing. Trial issuance and payment
require confirmed membership. Approved paid orders remain fulfilled if the buyer
leaves, but access is suspended until they rejoin. For reseller accounts the linked
end user is checked, otherwise the purchasing reseller is checked. Owner admins
are exempt; accounts without a Telegram owner cannot be checked.

## Activation

After deployment and a private SQLite backup, validate the bot is an administrator
of the intended channel. Store `membership_channel` (numeric channel ID),
`membership_url` (an existing valid `https://t.me/` invite link), then set
`membership_enabled=1`. The announcement-only `status_chat` setting is unchanged.
No credentials or real invite links belong in the repository.

The bot subscribes to `chat_member` updates and also reconciles every 60 seconds.
Requests waiting for channel join approval are not considered membership. API
errors or missing bot admin rights preserve existing customers' state; new trial
and payment requests wait for successful verification. Hot updates never restart
Xray for membership changes. Failed updates stay pending and retry. Private nodes
consume the same effective account set on their normal config-sync schedule.

Dates continue to run while access is suspended. UUIDs, usage, traffic limits,
wallets and orders are preserved. Joining never removes expiry, traffic, device
or manual restrictions. Status cards explicitly explain channel suspension.

## Reset trial eligibility

`shopdb.reset_trial_history()` resets only `customers.test_used` and returns the
affected row count. Take a consistent SQLite backup before calling it. Existing
trial accounts, orders, balances, referrals and customer records are preserved.
Each customer may then receive one new trial after joining the channel.

## Rollback

Owners can disable the feature under Management → Data and notifications →
Channel membership. Reconciliation removes only channel suspension within a
minute; all billing and manual restrictions remain. Membership state uses a
separate `channel_membership` table, keeping the original `users` schema compatible
with reverting this PR. Disable and reconcile before a code rollback so live Xray
access is also restored safely. To undo the one-time trial reset, restore only the
previous customers' `test_used` flags from the backup, not the entire live database
(which could overwrite subsequent sales).

Bot-only startup now writes the validated config and starts Xray only if it is
inactive, avoiding a restart of existing VPN connections during bot deployment.
On existing installs, `isaho update` / `install.sh` updates bot code only, keeps
previous code for rollback, and restores it if the service fails to start. It
does not run the Xray installer or change environment, firewall or networking.
Use `install.sh --full` only for separately approved infrastructure changes.
