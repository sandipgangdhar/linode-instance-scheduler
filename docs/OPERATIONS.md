<!--
docs/OPERATIONS.md -- CUSTOMER-FACING DISTRIBUTION

Day-2 operations reference for this Instance Scheduler deployment:
running the poller, backup & recovery, security operations, upgrading,
and troubleshooting. Read the project README first for what this is and
how to deploy it, and INSTANCE-SCHEDULER-DEFINITIVE-GUIDE.html for the
full architectural reference.

(c) Linode Instance Scheduler | Developed by Sandip Gangdhar | 2026
-->

# Operations Guide

## What you're running

Two long-lived processes matter for day-to-day operation:

- **The scheduler (`poll`)** — evaluates every managed instance's effective schedule on a
  short, fixed interval and fires a start or stop the moment a configured boundary is crossed.
  Nothing about manual `start`/`stop` requires this to be running, but no schedule or group
  membership is enforced automatically unless it is.
- **The API server (`serve-api`)** — serves the REST API and, once built, the web dashboard.
  Only needed if you want dashboard/API access; the CLI works independently of it.

Both are ordinary long-running processes meant to run under your own process supervisor, not
inside an interactive shell session.

### Running the scheduler under systemd

```ini
# /etc/systemd/system/instance-scheduler-poll.service
[Unit]
Description=Instance Scheduler - scheduler loop
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/instance-scheduler
ExecStart=/opt/instance-scheduler/.venv/bin/python3 instance_manager.py poll
Restart=always
RestartSec=15
StandardOutput=append:/var/log/instance-scheduler-poll.log
StandardError=append:/var/log/instance-scheduler-poll.log

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now instance-scheduler-poll.service
```

`Restart=always` plus `WantedBy=multi-user.target` covers both a process crash (auto-restart)
and a full host reboot (auto-start on boot) — confirm both with:

```bash
systemctl is-active instance-scheduler-poll.service     # should say "active"
systemctl is-enabled instance-scheduler-poll.service     # should say "enabled"
```

### Running the API server / dashboard under systemd

Same pattern, a second unit:

```ini
# /etc/systemd/system/instance-scheduler-api.service
[Unit]
Description=Instance Scheduler - API server & dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/instance-scheduler
ExecStart=/opt/instance-scheduler/.venv/bin/python3 instance_manager.py serve-api
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```

## Environment configuration reference

Configured via a single `.env` file in the deployment directory (copy `.env.example` and fill
in real values — never commit the real file).

| Variable | Required | What it does |
|---|---|---|
| `LINODE_API_TOKEN` | Yes | A full-access Linode Personal Access Token — the one credential that actually performs every create/delete/tag/volume operation. Used by the CLI, the scheduler, and the API server alike. |
| `LINODE_SSH_KEY_PATH` | No (has a default) | Path to this deployment's own SSH private key, used for every reachability check and `authorized_keys` capture. See the Definitive Guide's Security section for why this is one deployment-wide key, never a per-instance stored credential. |
| `LINODE_OAUTH_CLIENT_ID` / `LINODE_OAUTH_CLIENT_SECRET` | Only if using the dashboard's "Login with Linode" | Register your own OAuth Client in your own Linode account (Cloud Manager → Profile → OAuth Apps) — see "Setting up dashboard login" below. |
| `LINODE_OAUTH_REDIRECT_URI` | Only if using dashboard login | Must exactly match the redirect URI configured on the OAuth App above. |
| `API_ALLOWED_ORIGINS` | No | Comma-separated browser origins allowed to call the API cross-origin (CORS). Leave unset unless a separately-hosted frontend needs to call this API from a different origin. |

## Backup & recovery

### Backing up the registry

The entire local registry is one plain SQLite file, `state/instances.db`. There's no database
server to back up — a periodic file copy is a complete, valid backup:

```bash
cp state/instances.db /path/to/backup/instances-$(date +%F).db
```

Run this on whatever schedule matches your own risk tolerance (hourly, daily) via cron or a
timer unit. Also worth backing up: `state/known_hosts` (the tool's own SSH trust store — losing
it just means the next `start` for each instance needs its host key re-established, not lost
data).

### Recovering from a lost or corrupted registry

If `state/instances.db` is deleted or genuinely corrupted (not a valid database file), the tool
detects this on its next attempted access and tells you exactly what to do. The short version:

```bash
# If the file exists but is corrupted, move it aside first (skip this step if it's simply gone):
mv state/instances.db state/instances.db.corrupted

# A fresh, empty database is created automatically the next time any command runs. Then:
python instance_manager.py rebuild
```

`rebuild` scans your account for the tags this tool writes onto every managed resource and
reconstructs the registry from them — no dependency on the deleted file. An instance that was
**running** at the moment the database was lost recovers completely, including its network
configuration and SSH access list. An instance that was **stopped** at that moment recovers
partially — its identity and resource associations come back, but network/SSH details aren't
knowable without a live boot, and `rebuild` will name it explicitly as `needs_manual_recovery`
rather than guess. Bring one of those the rest of the way back:

1. Boot it once from its recorded OS volume and reserved IP (via Cloud Manager, or the raw
   Linode API — the tool itself won't do this step for you, since it has no live network
   configuration to boot it with).
2. `python instance_manager.py reset-host-key --ip <its reserved IP>` — a fresh boot presents a
   genuinely new SSH host key, which needs to be explicitly trusted once.
3. `python instance_manager.py onboard --name <name> --instance-id <the booted instance's ID> --force`
   — hands full management back to the tool, capturing everything live from the now-running
   instance.

### Resolving a stuck per-instance lock

Every mutating operation on a given instance takes a lock for its duration. If a process is
killed mid-operation (a crashed process, a killed SSH session), the lock is released
automatically the moment that process exits — this is an OS-level file lock, not something the
tool has to remember to clean up. If you still see a `Configuration error: ... already
operating on '<name>'` refusal with nothing actually running against that instance:

```bash
python instance_manager.py clear-lock --name <name>
```

### Resolving an orphaned migration volume

A Path B migration (see the Definitive Guide, Part II.2) that's abandoned partway through can
leave its destination volume behind, still billing, with no instance ever using it:

```bash
python instance_manager.py migrate-orphans              # list every orphaned migration volume found
python instance_manager.py migrate-orphans --cleanup     # permanently delete them, after re-verifying each one is genuinely orphaned
```

## Security operations

### Setting up dashboard login ("Login with Linode")

1. In Cloud Manager: **Profile → OAuth Apps → Create an App.**
2. Set the callback URL to `https://<your-deployment-host>/oauth/callback` — this must exactly
   match `LINODE_OAUTH_REDIRECT_URI` in your `.env`.
3. Copy the generated Client ID and Client Secret into `LINODE_OAUTH_CLIENT_ID` /
   `LINODE_OAUTH_CLIENT_SECRET`.
4. Restart the API server (`systemctl restart instance-scheduler-api`).

This is entirely separate from `LINODE_API_TOKEN` — the OAuth login only ever identifies who's
calling; it's never used to talk to Linode directly. See the Definitive Guide's Security
section for the full reasoning.

### Rotating the SSH trust for an instance

Deliberate key rotation — after infrastructure changes, or simply as routine hygiene — always
goes through the tool, never by hand-editing `state/known_hosts`:

```bash
python instance_manager.py reset-host-key --name <name>
# or, for an instance not yet under management, by address:
python instance_manager.py reset-host-key --ip <address>
```

A steady-state `start` that encounters an *unexpected* host-key change (one you didn't just
deliberately trigger) is refused outright as a `SECURITY WARNING` — see Troubleshooting below.

### Rotating the Linode API token

Generate a new full-access Personal Access Token in Cloud Manager, update
`LINODE_API_TOKEN` in `.env`, and restart both the scheduler and API server processes so they
pick up the new value (this token is read once, at process startup — editing `.env` alone
changes nothing until the process restarts).

## Upgrading

1. Read the release notes for the version you're upgrading to.
2. Back up `state/instances.db` first (see above) — cheap insurance before any upgrade.
3. Pull the new release, reinstall dependencies (`pip install -r requirements.txt`, and
   `npm ci && npm run build` under `web/` if you run the dashboard).
4. Restart both services:
   ```bash
   systemctl restart instance-scheduler-poll instance-scheduler-api
   ```
5. Confirm both came back up clean:
   ```bash
   systemctl status instance-scheduler-poll instance-scheduler-api   # both "active (running)"
   python instance_manager.py list                                   # registry still reads correctly
   ```

The database schema is migrated automatically and idempotently on first connection after an
upgrade — no separate migration step to run by hand.

## Monitoring & health checks

There's no separate metrics pipeline — the signal to watch is the tool's own state and logs:

- **Are both processes actually running?** `systemctl is-active instance-scheduler-poll
  instance-scheduler-api`.
- **Is the fleet in the state you expect?** `python instance_manager.py list` — cross-check any
  instance that's `stopped` when you didn't expect it against `history --name <name>` (a
  scheduled stop shows `triggered_by=schedule` in the audit trail; if you don't see that, it
  didn't come from this tool's own scheduling).
- **Did a scheduled action actually fire when it should have?** `python instance_manager.py
  history --name <name> --limit 10` shows the real recorded outcome, not just whether the
  poller is running.
- **Any instance stuck `needs_manual_recovery`?** `list` shows this status plainly — see
  "Recovering from a lost or corrupted registry" above for the fix.

## Troubleshooting

| Symptom | Most likely cause | What to check / do |
|---|---|---|
| Instance shows `unreachable` | Created successfully but the network reachability check hasn't yet succeeded, or the instance was powered off out-of-band (e.g. directly via Cloud Manager, not through this tool) | Run `start` again — the tool detects a confirmed-gone or genuinely powered-off instance and recovers automatically; a second `start` completes the recovery rather than creating a duplicate |
| A scheduled action didn't fire | The scheduler process (`poll`) isn't actually running, or the instance's effective schedule isn't what you expect | `systemctl is-active instance-scheduler-poll`; `python instance_manager.py schedule-show --name <name>`; confirm whether an individual schedule is silently overriding a group's (Part III.1 of the Definitive Guide) |
| `SECURITY WARNING` on `start` | The instance is presenting a different SSH host key than the one on record — could be a genuine security event, or simply an infrastructure change you made yourself (a rebuild via Cloud Manager, a disk swap) | Independently confirm the new key is legitimate (Lish console) before running `reset-host-key` — never bypass this check casually |
| `onboard` refuses with "still on local disk" | The instance hasn't been migrated onto a Block Storage volume yet | Run the Path B migration (`migrate-start` / `migrate-resume`) first — see the Definitive Guide, Part II.2 |
| `onboard` refuses with "IP not reserved" | The instance's public IP is still an ordinary, ephemeral one | Reserve it first via Cloud Manager, or use the one-time in-flow "reserve and retry" option on the dashboard's onboarding screen |
| A stop/start command refuses with "already operating on" | A per-instance lock is held — normally released automatically when the holding process exits | Confirm nothing is actually mid-operation on that instance, then `clear-lock --name <name>` |
| Group's timezone or schedule doesn't seem to apply | An individual schedule exists and is silently overriding the group entirely (working as designed, not a bug) | `schedule-show --name <name>` to check for an individual schedule; clear it if the group should govern instead |
| A configured schedule's savings percentage looks wrong | The schedule is disabled, or a manual-override auto-revert cycle is skewing actual vs. scheduled uptime | Check `schedule-show`'s `enabled` field; compare scheduled vs. actual savings — a persistent gap usually means overrides are happening more than expected |

## Reference

- `INSTANCE-SCHEDULER-DEFINITIVE-GUIDE.html` — full architectural reference: how the delete/
  recreate cycle works, the two onboarding paths, scheduling and override precedence, the REST
  API, security model, and deployment model.
- `python instance_manager.py <command> --help` — authoritative, always-current flag reference
  for any command in this guide.
