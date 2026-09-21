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

## Installing and configuring

The tool is a plain Python program with no database server or other infrastructure of its own
to stand up — cloning the repository, creating a virtual environment, and installing its
dependencies is the entire install. Run this on one centralized, persistent machine your whole
team uses (not a personal laptop) — the local registry lives only on whatever machine runs
these commands, so a second, disconnected clone on someone else's laptop would have no idea
what's already onboarded:

```bash
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler/spike
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Configuration is one flat file, `.env`, read once at process startup:

```bash
cp .env.example .env
```

Open `.env` and fill in `LINODE_API_TOKEN` — a full-access Linode Personal Access Token, the
only setting required to run anything at all. Every other variable is optional and only matters
if you're using the specific feature it enables — see "Environment configuration reference"
below for the full list.

Confirm the install itself is working (a purely local check, no token required yet):

```bash
python instance_manager.py list
```

This prints `No instances onboarded yet.` (or a real list, if some are already onboarded) once
the environment and dependencies are set up correctly. To confirm the token itself is valid:

```bash
python -c "import linode_engine as e; c = e.build_client(e.load_token()); e.auth_check(c); print('token OK')"
```

From there, onboarding your first instance and running the scheduler/API server as supervised
background processes (below) is the rest of getting to a genuinely running deployment. See the
project README for the fuller first-node walkthrough (creating a node, attaching storage,
Path B migration if you're bringing an existing instance under management) and the Definitive
Guide's own Deployment Model chapter for why installation looks like this.

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

### High availability — one supervised process, not active-active or active-passive

Run exactly **one** instance of `poll`, ever — never two running at once, and never a hot
standby waiting to take over. This is a deliberate design choice, not a gap:

- **The scheduler doesn't serve live traffic.** It evaluates schedules on a short, fixed
  interval with a tolerant matching window (typically a few minutes either side), not on every
  incoming request. If the process is briefly down — a crash, a host reboot, a deploy — the
  next due action just fires a few seconds late once your process supervisor restarts it.
  Nothing downstream is waiting on it in real time, so nothing breaks. Manual `start`/`stop`
  (CLI or API) keeps working the entire time regardless, since it doesn't depend on `poll`
  being up at all.
- **Active-active would trade that harmless delay for a real correctness risk.** Two scheduler
  processes with no genuine coordination between them (a distributed lock, leader election, a
  consensus protocol — the actual infrastructure "active-active" requires) could race to act on
  the same instance at the same moment, or have one fire a start while the other is mid-firing
  a stop for the same node. That's a new failure mode traded in to solve a problem — a few
  seconds of scheduler downtime — that a plain restart already solves for free.
- **Active-passive needs its own always-on standby, plus failure-detection machinery** (health
  checks, a way for the standby to agree it should take over, a shared or replicated view of
  state) to be worth anything — real infrastructure, kept running and paid for continuously, to
  protect against an outage a single supervised process already recovers from in seconds.
- **This is a cost decision as much as an architectural one.** The whole point of this product
  is eliminating payment for compute that isn't earning its keep. Running a second, always-on
  scheduler instance purely as insurance against a multi-second restart would mean paying for
  exactly the kind of idle, always-on capacity this tool exists to help you stop paying for —
  just moved onto the tool's own infrastructure instead of your managed fleet. The model used
  here costs nothing extra: one process, under whatever process supervisor already ships with
  your host OS, on the one machine you're already running this from.

If the underlying data is ever lost too (not just the process being briefly down), recovery
still doesn't need a second live system standing by — see "Backup & recovery" below for the
tag-based and Object Storage-backed recovery path, and the Definitive Guide's own Deployment
Model chapter for the fuller comparison against active-active/active-passive designs.

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
| `LINODE_OBJ_STORAGE_BUCKET` / `LINODE_OBJ_STORAGE_ENDPOINT` / `LINODE_OBJ_STORAGE_ACCESS_KEY` / `LINODE_OBJ_STORAGE_SECRET_KEY` | No | Optional Object Storage backup of each instance's full record, for fuller disaster recovery than tags alone provide. See "Setting up Object Storage backup" below. All four must be set together — leave all four unset to skip this layer entirely. |

## Backup & recovery

This tool already protects every managed instance's identity, schedule, and group membership
against total local database loss automatically, at no setup cost — see "How recovery actually
works" below. Object Storage is an *additional*, optional layer that closes the remaining gap
(recovering a stopped instance's network configuration and SSH access fully, not just partially)
and is worth setting up for anything beyond a purely disposable dev/test fleet.

### How recovery actually works

Two independent backups exist alongside the local database, and neither is something you have to
maintain by hand:

- **Linode's own resource tags** — every onboard and stop writes the instance's identity (OS
  volume, data volumes, reserved IP), individual schedule, group membership, a simple network
  configuration (if it's a single public interface), and a reference to any SSH key that's also
  registered on your Linode account, directly onto the resources themselves. This happens
  automatically, for every managed instance, with nothing to configure.
- **Object Storage** (optional) — the same operations also back up a complete record of
  everything tags can't hold: a more complex network configuration (multiple interfaces, VPC,
  VLAN), any SSH key not registered on your Linode account, and the instance's plan/firewall/
  placement-group/maintenance-policy/watchdog/label/tags. This layer only exists if you configure
  it (below).

Both are best-effort and self-healing: a failed backup never blocks the real stop/onboard
operation, and the very next time that instance is touched, its full current state is backed up
again from scratch — nothing needs to be manually retried. See the Definitive Guide, Part 5, for
the full mechanics, including exactly what's read from where during recovery and a complete
table of edge cases.

### Setting up Object Storage backup

1. In Cloud Manager: **Object Storage → Create Bucket.** Any region is fine; note the endpoint
   shown for it.
2. **Object Storage → Access Keys → Create Access Key.** Scope it to just this one bucket, not
   account-wide, if given the option.
3. Set all four variables in `.env`:
   ```bash
   LINODE_OBJ_STORAGE_BUCKET=your-bucket-name
   LINODE_OBJ_STORAGE_ENDPOINT=https://your-bucket-name.your-region.linodeobjects.com
   LINODE_OBJ_STORAGE_ACCESS_KEY=...
   LINODE_OBJ_STORAGE_SECRET_KEY=...
   ```
4. Restart both services so they pick up the new configuration:
   ```bash
   systemctl restart instance-scheduler-poll instance-scheduler-api
   ```

No backfill step is needed — the very next stop or onboard for each instance starts backing it up
from that point on. An instance that isn't touched again after this is configured won't have an
Object Storage backup until it is.

### Scheduling whole-system backups with `backup`

The entire local registry is one plain SQLite file, `state/instances.db` — there's no database
server involved, so a consistent snapshot of it is a complete backup on top of the two layers
above. Rather than copying the file directly (a raw copy against a live, in-use database risks
capturing a torn/inconsistent snapshot), use the tool's own `backup` command, which takes a
genuinely consistent snapshot via SQLite's own backup mechanism and, if Object Storage is
configured, also re-syncs every currently-onboarded instance's own Object Storage record in the
same pass — closing the gap for any instance that hasn't individually been stopped or onboarded
in a while:

```bash
python instance_manager.py backup --backup-dir /path/to/backup
```

Give it `--backup-dir`, rely on Object Storage alone, or both — it refuses with a clear error if
neither is configured, since there'd be nothing to actually do. Run it on whatever schedule
matches your own risk tolerance via cron or a systemd timer:

```ini
# /etc/systemd/system/instance-scheduler-backup.service
[Unit]
Description=Instance Scheduler - whole-system backup

[Service]
Type=oneshot
WorkingDirectory=/opt/instance-scheduler
ExecStart=/opt/instance-scheduler/.venv/bin/python3 instance_manager.py backup --backup-dir /var/backups/instance-scheduler
```

```ini
# /etc/systemd/system/instance-scheduler-backup.timer
[Unit]
Description=Run the Instance Scheduler backup daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload
systemctl enable --now instance-scheduler-backup.timer
```

The command exits non-zero if anything didn't complete (a per-instance Object Storage re-sync
failure, either snapshot destination failing) — wire this into your own monitoring via the exit
code, not just by reading its log output. Also worth backing up, by whatever means you already
use for plain files: `state/known_hosts` (the tool's own SSH trust store — losing it just means
the next `start` for each instance needs its host key re-established, not lost data).

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
reconstructs the registry from them, then consults Object Storage (if configured) to fill in
anything tags alone can't hold — no dependency on the deleted file either way. An instance that
was **running** at the moment the database was lost always recovers completely, including its
network configuration and SSH access list, read fresh off the live instance. An instance that was
**stopped** at that moment recovers as fully as what's available: if its network configuration
was simple enough to tag-encode, or an Object Storage backup exists for it, or both, it comes
back fully usable — `stopped`, ready for a normal `start`, no manual step required. Only if
neither source had what was needed does it come back flagged `needs_manual_recovery`. Bring one
of those the rest of the way back:

1. Boot it once from its recorded OS volume and reserved IP (via Cloud Manager, or the raw
   Linode API — the tool itself won't do this step for you, since it has no live network
   configuration to boot it with).
2. `python instance_manager.py reset-host-key --ip <its reserved IP>` — a fresh boot presents a
   genuinely new SSH host key, which needs to be explicitly trusted once.
3. `python instance_manager.py onboard --name <name> --instance-id <the booted instance's ID> --force`
   — hands full management back to the tool, capturing everything live from the now-running
   instance.

If an instance keeps coming back `needs_manual_recovery` after a rebuild and you expected it to
recover fully, the most common cause is that Object Storage wasn't configured (or wasn't yet
backed up for that specific instance) at the time local state was lost — see "How recovery
actually works" above, and the Definitive Guide's Part 5.6 for the complete list of edge cases
and exactly what's at risk in each.

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
| `rebuild` recovered an instance as `needs_manual_recovery` instead of `stopped` | Neither tags nor Object Storage had a complete-enough record for it at the moment of loss — most commonly, Object Storage wasn't configured yet, or that specific instance hadn't been stopped/onboarded since it was | See "Recovering from a lost or corrupted registry" above for the manual-boot recovery steps, and Part 5.6 of the Definitive Guide for the full edge-case table |
| Group's timezone or schedule doesn't seem to apply | An individual schedule exists and is silently overriding the group entirely (working as designed, not a bug) | `schedule-show --name <name>` to check for an individual schedule; clear it if the group should govern instead |
| A configured schedule's savings percentage looks wrong | The schedule is disabled, or a manual-override auto-revert cycle is skewing actual vs. scheduled uptime | Check `schedule-show`'s `enabled` field; compare scheduled vs. actual savings — a persistent gap usually means overrides are happening more than expected |

## Reference

- `INSTANCE-SCHEDULER-DEFINITIVE-GUIDE.html` — full architectural reference: how the delete/
  recreate cycle works, the two onboarding paths, scheduling and override precedence, the REST
  API, security model, and deployment model.
- `python instance_manager.py <command> --help` — authoritative, always-current flag reference
  for any command in this guide.
