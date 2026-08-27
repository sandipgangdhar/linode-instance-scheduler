# Deployment Guide — running this in production

This is the operations-focused companion to `README.md`. That document covers the day-to-day
commands (onboarding a node, setting a schedule, using the API); this one covers standing the
tool up as a persistent, unattended service: choosing a host, installing it, configuring
credentials, running the scheduler and API as real services that survive a reboot, backups, and
upgrades.

It assumes you've already read `README.md` §1–§4 (what this is, prerequisites, building or
migrating your first node) and have at least one node ready to manage. If you haven't done that
yet, start there — this guide is about running the tool itself, not about the Linode instances it
manages.

---

## 1. Deployment model — what you're actually setting up

This tool is **self-hosted, one deployment per Linode account** — there's no shared service, no
multi-tenant backend, nothing running outside infrastructure you control. A deployment is:

- **One host** (a small Linode instance works fine for this — see §2) running the Python code in
  this repository.
- **A local SQLite database** (`state/instances.db`) that's the source of truth for which
  instances/volumes/IPs belong to which logical name, what their schedules are, and their audit
  history. This is genuinely local state, not a mirror of something else — see §8 for why that's
  safe and how to back it up.
- **Up to three long-running pieces**, all optional except the first if you want automation at
  all:
  1. **The scheduler (`poll`)** — the process that actually fires `start`/`stop` when a schedule
     says it's due. Nothing is automated without this running somewhere.
  2. **The REST API (`serve-api`)** — optional; only needed if you want a web dashboard or your
     own scripts talking to this tool over HTTP instead of shelling out to the CLI.
  3. **The web dashboard** — optional; a static build served by the API process, not a separate
     service.

If you only ever plan to run commands by hand from a terminal, you don't need any long-running
service at all — just the CLI, run whenever you need it. This guide is for the common case where
you want schedules to actually enforce themselves unattended.

---

## 2. Choosing and preparing a host

**Requirements:**
- **Python 3.10 or newer.**
- **Outbound HTTPS access** to `api.linode.com` (every real operation) and, if you use the REST
  API with "Login with Linode," `login.linode.com` too. No inbound access is required unless
  you're exposing the REST API/dashboard to other people (§6).
- **Node.js 18+**, only if you're building the web dashboard (§7) — not needed for the CLI or
  poller alone.
- A **persistent, centralized machine** — not a laptop that sleeps or gets reimaged. A small,
  always-on Linode instance is a natural, if slightly self-referential, choice: nothing about
  running this tool's own host through this tool itself is required or recommended — keep the
  deployment host as a normal, separately-managed instance.

A single small instance (the cheapest general-purpose plan) is more than enough — this tool's own
resource use is minimal (a Python process ticking every few minutes, a small SQLite file, an
optional lightweight API server). Sizing should be driven by how many *managed* instances you
have, not by this tool's own overhead, and even fleets of dozens of managed nodes don't meaningfully
change that.

Create the deployment host itself the normal way (Cloud Manager, or your own existing
provisioning process) — nothing here is specific to it beyond the requirements above.

---

## 3. Installing the code

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Pick a real, deliberate install location — e.g. `/opt/linode-instance-scheduler` — rather than a
home directory, so the path is stable across whoever administers this host over time. The
commands and systemd units below assume this repository lives at a fixed path; substitute your
own throughout.

---

## 4. Configuring credentials

```
cp .env.example .env
chmod 600 .env
```

`chmod 600` matters here — `.env` holds real, sensitive credentials (see below), and by default a
freshly-copied file is often group/world-readable depending on your host's umask. This file is
already gitignored; never commit it, and never paste its contents anywhere outside this host.

Fill in:

- **`LINODE_API_TOKEN`** — a Personal Access Token scoped to **Linodes** (Read/Write), **Volumes**
  (Read/Write), and **IPs** (Read/Write); **Account** (Read Only) is enough to satisfy the tool's
  own startup check. Create one at Cloud Manager → Profile → API Tokens. This is the one
  credential every part of the tool uses to actually talk to Linode — the CLI, the poller, and the
  API server alike.
- **`LINODE_OAUTH_CLIENT_ID`/`LINODE_OAUTH_CLIENT_SECRET`/`LINODE_OAUTH_REDIRECT_URI`** — only
  needed if you're running the REST API/dashboard with "Login with Linode" (§6). Leave blank if
  you're only ever using the CLI and/or poller.
- **`LINODE_SSH_KEY_PATH`** — optional; only needed if your SSH keypair (see `README.md` §2)
  isn't at the tool's default path (`~/.ssh/linode_spike_key`). The CLI's own `--ssh-key` flag and
  this variable both point at the same default, so if you generated your key at the default path
  you don't need to set this at all.
- **`API_ALLOWED_ORIGINS`** — only needed if a browser-based client (the dashboard, or your own
  frontend) will call the REST API from a *different* origin than the one serving it. Leave unset
  to disable CORS entirely — correct for the common case where the dashboard is served by the same
  `serve-api` process it calls.

**Verify it's all wired up correctly** before moving on:

```
python instance_manager.py list
```

This should print `No instances onboarded yet.` (or a real list, if you've already onboarded
some) — it's a purely local check and doesn't touch the Linode API at all, so it only confirms
your Python environment, not your token. To check the token itself:

```
python -c "import linode_engine as e; c = e.build_client(e.load_token()); e.auth_check(c); print('token OK')"
```

---

## 5. Running the scheduler as a persistent service

Nothing is automated until `poll` is actually running continuously somewhere. Run it under a real
process supervisor so it survives crashes and reboots — don't run it in a bare terminal/`screen`
session for anything beyond a quick test.

**systemd unit** (`/etc/systemd/system/linode-scheduler-poll.service`):

```ini
[Unit]
Description=Linode Instance Scheduler - poller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=linode-scheduler
WorkingDirectory=/opt/linode-instance-scheduler
EnvironmentFile=/opt/linode-instance-scheduler/.env
ExecStart=/opt/linode-instance-scheduler/.venv/bin/python instance_manager.py poll
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Create a dedicated, unprivileged system user to run this as (`useradd --system --home
/opt/linode-instance-scheduler --shell /usr/sbin/nologin linode-scheduler`), and make sure it owns
the repository directory (`chown -R linode-scheduler:linode-scheduler
/opt/linode-instance-scheduler`) — this process holds your Linode API token in memory and manages
real infrastructure, so it shouldn't run as `root` or as an interactive user's own account.

```
sudo systemctl daemon-reload
sudo systemctl enable --now linode-scheduler-poll
sudo systemctl status linode-scheduler-poll
journalctl -u linode-scheduler-poll -f     # live logs — this is where poll's own output goes
```

`Restart=always` is the deliberate choice here, not a stronger form of high availability: this
project's own design is "fast, safe recovery," not "never goes down" (see `README.md`'s own
troubleshooting notes on this) — a brief restart after a crash self-heals, and running two active
pollers at once is a correctness risk (both could race to fire the same schedule), not a safety
net. Don't scale this to more than one instance.

**Prefer cron over a supervisor?** `poll --once` runs exactly one check and exits — schedule that
on whatever interval you'd otherwise poll on (a 5-minute cron entry matches the poller's own
default). Either approach is fine; pick whichever this host's existing operational conventions
already use.

Every schedule edit (via the CLI or, if you run it, the API) takes effect on the poller's *next*
tick automatically — there's nothing to reload or restart when you change a schedule.

---

## 6. Running the REST API and dashboard as a persistent service (optional)

Skip this section entirely if you only use the CLI and poller.

**systemd unit** (`/etc/systemd/system/linode-scheduler-api.service`):

```ini
[Unit]
Description=Linode Instance Scheduler - REST API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=linode-scheduler
WorkingDirectory=/opt/linode-instance-scheduler
EnvironmentFile=/opt/linode-instance-scheduler/.env
ExecStart=/opt/linode-instance-scheduler/.venv/bin/python instance_manager.py serve-api --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable --now linode-scheduler-api
```

**Binds to `127.0.0.1` by default, deliberately.** Nothing outside this host can reach it directly
— that's intentional, not a bug to work around by binding `0.0.0.0` and exposing it raw. Put a
real reverse proxy in front for anything beyond local testing, both for TLS (real "Login with
Linode" OAuth callback URLs must be `https://`) and so the API/dashboard aren't the first thing an
attacker's port scan finds.

**Example using Caddy** (simplest option — automatic HTTPS via Let's Encrypt, no manual
certificate management):

```
your-deployment-host.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

That's the entire config — Caddy handles obtaining and renewing the TLS certificate on its own.
If you already run nginx or another proxy for other services on this host, a plain
`proxy_pass http://127.0.0.1:8000;` block works identically; the tool has no opinion on which
proxy you use.

**Registering "Login with Linode" OAuth** (skip if you'll only ever call the API with your own
tooling and don't need browser login):

1. Cloud Manager → Profile → OAuth Apps → Create an App. Callback URL:
   `https://your-deployment-host.example.com/oauth/callback` — must match your `.env`'s
   `LINODE_OAUTH_REDIRECT_URI` **exactly**, including the scheme (`https://`, once you have a real
   proxy/TLS in front — see above).
2. Copy the **Client ID** and **Client Secret** into `.env` (§4).
3. Restart the API service (`sudo systemctl restart linode-scheduler-api`) so it picks up the new
   values.
4. Visit `https://your-deployment-host.example.com/login` and confirm a real login completes and
   lands you on the dashboard (or, if you haven't built it yet, a plain JSON pointer at `/docs` —
   see §7).

Each deployment registers its **own** OAuth App, in its **own** Linode account — there's no shared
login service any other deployment of this tool uses.

---

## 7. Building the web dashboard (optional)

Only relevant if you're running the REST API (§6). Requires Node.js 18+ **on the machine doing the
build** — this can be the deployment host itself, or your own laptop/CI, since only the build
*output* needs to reach the deployment host.

**Building on the deployment host directly** (simplest):

```
cd web
npm install
npm run build
cd ..
sudo systemctl restart linode-scheduler-api
```

`npm run build` produces `web/dist/`; `serve-api` serves it automatically at `/` and `/ui/*` the
next time it starts, with no separate configuration. If `web/dist/` doesn't exist yet, `/` degrades
gracefully to a JSON pointer at `/docs` (the API's own interactive documentation) instead of a
broken page — so it's safe to skip this section entirely and add the dashboard later.

**Building elsewhere and deploying just the output** — if you'd rather not install Node on the
production host at all: run the same `npm install && npm run build` on your own machine, then copy
the resulting `web/dist/` directory to the same path on the deployment host (`rsync -av web/dist/
your-host:/opt/linode-instance-scheduler/web/dist/`), and restart the API service there.

Re-run the build (and restart the service) after pulling a new version of this repository if
`web/src/` changed — see §9.

---

## 8. Backups and disaster recovery

`state/instances.db` is a real SQLite database and the source of truth for schedules, group
membership, and which Linode resources (volumes, reserved IPs) belong to which logical name.
Losing it isn't catastrophic — every managed instance's identity is *also* mirrored onto that
instance's own Linode tags specifically so it can be reconstructed — but a routine backup makes
the common case (accidental deletion, disk corruption) a fast restore instead of a full rebuild.

**Routine backup** — a safe, hot backup (works even while the poller/API are actively writing to
the database), via a simple cron entry:

```
# /etc/cron.d/linode-scheduler-backup
0 * * * * linode-scheduler sqlite3 /opt/linode-instance-scheduler/state/instances.db ".backup '/opt/linode-instance-scheduler/backups/instances-$(date +\%Y\%m\%dT\%H\%M).db'"
```

Adjust the frequency and retention (add a second line pruning old backups) to your own comfort
level, and copy backups off this host periodically (a Linode Object Storage bucket, another
instance) so they don't share a single point of failure with the deployment host itself.

**If `state/` is lost or corrupted entirely** — restore your most recent backup file to
`state/instances.db` and restart both services. **If no backup exists at all** (or you want to
confirm reconstruction works before you ever need it), the tool's own `rebuild` command
reconstructs the registry directly from tags already written onto your Linode resources:

```
python instance_manager.py rebuild
```

This recovers instance identity, individual schedules, and group membership for every previously
onboarded, still-existing resource — reach for it as the guaranteed last resort, not the routine
path (a routine restore from a recent backup is faster and loses less). One real limitation worth
knowing before you need it: a schedule **group with zero members** at the moment of data loss has
no tag trace anywhere and can't be reconstructed this way — if that's a real concern, make sure
groups keep at least one member, or lean on the routine backup above instead.

---

## 9. Upgrading

```
cd /opt/linode-instance-scheduler
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart linode-scheduler-poll linode-scheduler-api   # the second only if you run it
```

If `web/src/` changed in the update, also rebuild the dashboard (§7) before restarting the API
service. The database schema upgrades itself automatically and safely on first connection after
an update — there's no separate migration step to run by hand.

---

## 10. Security checklist

- `.env` is `chmod 600`, owned by the same unprivileged user the services run as, and never
  committed to version control (already gitignored).
- The dedicated SSH keypair (`README.md` §2) is used **only** for this tool — never a personal or
  shared key — so it can be revoked independently without affecting anything else.
- `LINODE_API_TOKEN` is scoped to exactly what's needed (§4), not a token with broader account
  access than this tool actually uses.
- The REST API binds to `127.0.0.1` and sits behind a real TLS-terminating reverse proxy (§6) —
  never exposed directly on a public interface.
- If an OAuth Client Secret or API token is ever pasted somewhere it shouldn't have been (a chat
  log, a support ticket, a shared document), rotate it: Cloud Manager lets you reset an OAuth
  App's secret and revoke/recreate a Personal Access Token independently, with no code changes
  needed here beyond updating `.env` and restarting the affected service.
- Run services as a dedicated unprivileged system user (§5), never as `root`.
