# Linode Instance Scheduler — Setup & Usage Guide

Linode Instance Scheduler runs your Linode instances only when you need them — on a schedule, by
hand, or when someone asks for them — and stops paying for their compute the rest of the time,
while each instance keeps its IP addresses, its disks and its identity.

This guide walks you through everything needed to use it, end to end: preparing a node, installing
the tool, bringing existing nodes under its management, and running it day to day (schedules,
groups, start order, hooks, wake on demand, budgets, backups and recovery).

It assumes no prior familiarity with this tool. If you follow it top to bottom on a real node,
you'll end with that node starting and stopping on its own.

---

## 1. What this is, and why it exists

On most clouds, stopping an instance you don't need right now — a development environment
overnight, a test system at the weekend, a standby that waits for an incident — stops its compute
charges. **That doesn't work on Linode as-is**, because of one billing difference:

> A *stopped* Linode instance bills at the same rate as a running one. Powering it off does not
> stop charges. The only thing that actually stops compute billing is fully **deleting** the
> instance.

So a literal "stop" isn't the right tool here. This tool instead **deletes the instance when you
don't need it, and recreates it the moment you do** — but it's built so that from your side, it
behaves exactly like stop/start:

- The **public IP never changes** across cycles (it's reserved to your account, not tied to the
  instance's lifecycle), and VPC and VLAN addresses are kept too — no DNS or allow-list updates
  needed.
- **Data is never lost** — the system disk and data disks live on Block Storage volumes,
  independent of the instance, and survive every delete/recreate cycle.
- The **SSH host key stays stable** — no "remote host identity changed" warnings when you
  reconnect after a recreate.
- **Label, tags, plan/size, firewall, and other instance settings** are all captured and
  reapplied automatically, so the recreated instance looks identical in Cloud Manager and in
  your cost reports — not like a brand-new resource.

While "stopped," you pay a small ongoing fee for the Block Storage volume(s) and for the reserved
IP sitting idle — far cheaper than running compute 24/7, but not literally free. See
[§9, Costs](#9-costs) for the numbers.

### What you can do with it

- **Schedules.** Run a node, or a whole group of nodes, only during the hours you choose — for
  example 09:00–18:00 on weekdays in your own timezone — with holidays skipped. A node can follow
  its group's schedule or have its own.
- **Start order.** Start a database group before the application group that needs it, and stop
  them in reverse, waiting for each to be up and ready.
- **Hooks.** Run your own command on a node before it stops (flush a database, drain a queue) or
  after it starts (wait until a service is really ready).
- **Wake on demand.** Keep a node stopped until someone needs it — a secret link, or a visitor to
  your site through a NodeBalancer, starts it — and stop it again once it's unused.
- **Manual control.** Start, stop or extend from the dashboard, the CLI, the REST API with scoped
  tokens, or Slack/Teams; a manual start outside the schedule stops by itself after a while.
- **Keeping costs in check.** See what each schedule saves, set monthly running-hour budgets and
  guardrails, and get suggested schedules learned from real usage.
- **Standbys.** Keep pre-configured nodes — failover standbys, extra replicas — stopped until an
  incident or a traffic spike, then start them in minutes at their usual addresses.
- **Recovery.** Everything needed to rebuild the tool's own state is kept in Linode tags and,
  optionally, in your Object Storage bucket, so losing the scheduler's host loses nothing.

Every one of these uses the same underlying idea: a named node that's brought up and torn down,
always coming back identical. `why --name <node>` tells you at any time why a node is running or
stopped.

---

## 2. Prerequisites

Before you start, make sure you have:

- **A Linode account** with permission to create instances, volumes, and reserved IPs.
- **A Linode Personal Access Token** — Cloud Manager → Profile (top right) → API Tokens →
  "Create a Personal Access Token." Scopes needed: **Linodes** (Read/Write), **Volumes**
  (Read/Write), **IPs** (Read/Write) and **StackScripts** (Read/Write, for the recovery copy of
  the SSH key's passphrase). Add **VPCs** (Read) too if any of your instances use a VPC
  interface. **Account** (Read Only) is enough to satisfy the tool's own startup check, and lets
  it confirm that dashboard sign-ins belong to your account's users. Add **Events** (Read Only)
  for `diagnose`, **Firewalls** (Read/Write) if instances have Cloud Firewalls, **NodeBalancers**
  (Read/Write) if nodes sit behind a NodeBalancer, and **Kubernetes** (Read/Write) only for LKE
  node pools. Account (Read/Write) is optional: it only lets the tool add its SSH key to the
  token owner's profile (see `DEPLOYMENT.md`). Create it from a dedicated
  Linode user (e.g. `instance-scheduler`) rather than a person's login: Linode records every
  change under the token's owner, so its events then tell the tool's changes apart from changes
  people make in Cloud Manager.
- **Access to this GitHub repository** (you should already have this).
- **A dedicated SSH key pair** for this automation to use. You don't need to make one yourself:
  the installer (§4) generates it at `/opt/linode-instance-scheduler/keys/deploy_key` (or uses
  the private key you give it with `--ssh-key PATH`), records it as `LINODE_SSH_KEY_PATH` in
  `.env`, and adds its public half (`keys/deploy_key.pub`) to your Linode profile's SSH keys.
  Don't reuse a personal key you use for other things — this one should be scoped to the tool
  and easy to revoke on its own. Every node you want this tool to manage needs that public key
  in root's `authorized_keys` (§3.1).
- **Nodes on a supported OS image.** Every Linode distribution image with `cloud-init` and
  Akamai's datasource works: Ubuntu 22.04/24.04/26.04, Debian 12/13, Kali, Rocky Linux 8/9/10,
  AlmaLinux 8/9/10, CentOS Stream 9/10, Fedora 43/44, openSUSE Leap 16.0, Alpine 3.21/3.24, Arch
  and Gentoo. Slackware isn't supported (its image has no `cloud-init`). The tool re-applies each
  node's network settings in whatever format that OS uses (systemd-networkd, NetworkManager,
  wicked, `/etc/network/interfaces` or netifrc) every time it recreates the node.
- **A 64-bit x86 Linux host with systemd** to run this from (Ubuntu 22.04+, Debian 12+,
  RHEL/Rocky/Alma 9+ or similar) — a **centralized, persistent server** (a dedicated admin box or
  bastion host), not someone's personal laptop. See §4 for why. The tool is a single compiled
  program; nothing else (no Python, no Node.js) needs to be installed.

---

## 3. Building a node the normal way

This section is what you'd do today with no automation involved: build an ordinary Linode node
running your application. If you already have nodes you want to schedule, skip to
[§4](#4-installing-and-configuring-the-automation) and come back to
[§6](#6-one-time-migration-onto-block-storage-path-b) for what's different about bringing an
*existing* node under this tool's management.

### 3.1 Create the Linode instance

In Cloud Manager (or via the API/CLI, if you prefer): create a Linode the normal way — pick a
region, a plan sized for your workload, and any Linux image listed under "Nodes on a supported OS
image" in §2. Nothing special is required at this stage — this is an entirely
ordinary Linode, local disk and all.

Add the automation's SSH public key (from [§2](#2-prerequisites)) to the instance at creation
time (it's in your profile, so it appears in Cloud Manager's "SSH Keys" field), or add it
afterward from the scheduler host:

```
ssh-copy-id -i /opt/linode-instance-scheduler/keys/deploy_key.pub root@<instance-ip>
```

**This step matters**: the automation needs to be able to SSH into your node using this key, both
to inspect it during onboarding and to check it's reachable after every recreate. The node's own
`authorized_keys` file lives on its disk and stays as it is across every recreate, so keys you add
or remove later are kept. If you'd rather not add the key yourself, the dashboard's onboarding
can log in once with a root password or private key you give it and install the key for you
(nothing you give it is stored).

### 3.2 Install and configure your application

Install and configure whatever the node runs — a web or application server, a database, a build
runner, a development environment — exactly as you normally would. Nothing about how you
configure or operate it changes because of this tool.

The one thing that matters for this tool is **where the application keeps data that must
survive**: databases, uploads, caches you want to keep warm. Put it on a Block Storage data
volume (next step) or on the node's own system disk — both survive every stop/start. Make sure
the services you need start on boot (`systemctl enable <service>`), since every start is a real
boot.

### 3.4 Attach a Block Storage volume for your data

In Cloud Manager: create a volume sized for your data, in the **same region** as the instance,
and attach it. Then, on the instance:

```
# Confirm the device path Linode assigned it (usually /dev/sdc on a fresh image-deployed instance)
lsblk

# Format it (only once, on a brand-new volume — skip this if it already has a filesystem)
mkfs.ext4 /dev/sdc

# Mount it
mkdir -p /mnt/data
mount /dev/sdc /mnt/data
```

**Add it to `/etc/fstab` so it remounts automatically on boot — using the stable by-id path, with
`nofail`:**

```
ls -la /dev/disk/by-id/ | grep sdc     # find the scsi-0Linode_Volume_<label> entry for your volume
echo '/dev/disk/by-id/scsi-0Linode_Volume_<your-volume-label>  /mnt/data  ext4  defaults,nofail  0  2' >> /etc/fstab
```

**Why `nofail` matters, specifically**: without it, a data volume that isn't attached yet at the
exact moment the boot sequence checks `/etc/fstab` can hang the entire boot — which would lock
you out of SSH on every single recreate. `nofail` tells the boot process to continue even if
this particular mount isn't ready yet, and it always catches up correctly once the volume is
actually attached (which happens before the OS boots on every recreate this tool performs).

Point your application's data directory at `/mnt/data`, and restart the service so it's actually
using it.

At the end of this section, you have a completely ordinary, real, running node — your application,
your normal config, a data volume with real data on it, reachable at whatever public IP Linode
assigned it. Nothing about it is special yet; this is exactly the starting point the automation
expects.

---

## 4. Installing and configuring the automation

This section covers the default deployment: a single, persistent VM. If you already run a
Linode Kubernetes Engine (LKE) cluster and would rather run this tool there instead, see
`DEPLOYMENT-LKE.md` instead of this section — same tool, same commands, just packaged as a
container.

**Run this from a centralized server, not your local laptop.** This tool keeps track of every
node it manages in a local registry file on whatever machine you run it from (see §8's `rebuild`
section for the full reasoning). If you run it from your own laptop, that registry only exists
there — a colleague running the same commands from *their* laptop would have no idea those nodes
are already onboarded, and you'd end up with two disconnected, out-of-sync views of the same
infrastructure. Pick one small, persistent machine (a dedicated admin box, a bastion host,
whatever you already use for this kind of operational tooling) that your whole team runs this
from, so there's exactly one source of truth for what's onboarded and what state it's in. The
`rebuild` command (§8) exists as a safety net if that machine is ever lost — it isn't a
substitute for having a single, agreed-upon place to run this from in the first place.

Install it with `install.sh` from the repository. It downloads the latest release for this host
(checking its published SHA-256 checksum), creates a service user, asks for your API token and
the other settings, and starts the scheduler and the web dashboard as services:

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler
sudo ./install.sh install
```

When it asks for the API token, give it the one from [§2](#2-prerequisites). It stores the
settings in `/opt/linode-instance-scheduler/.env` (readable only by the service user — never paste
your token into chat, a commit, or anywhere else). `DEPLOYMENT.md` explains every step and option,
including serving the dashboard over HTTPS (`--domain`).

Verify it:

```
sudo linode-instance-scheduler list
sudo linode-instance-scheduler token-check
```

`list` should print `No instances onboarded yet.` (or a list, if you already have some) — it's a
purely local command, so it works before anything else is configured. `token-check` prints
`token OK` when the API token works; if it doesn't, check the token's scopes, fix the
`LINODE_API_TOKEN` line in `.env`, and run it again.

---

## 5. The commands, what each one is for

Every command below is run on the host you installed on, as
`sudo linode-instance-scheduler <command>` — `sudo` runs it as the service user, against the same
database and settings the services use. (In Kubernetes:
`kubectl exec -n linode-instance-scheduler deploy/linode-instance-scheduler -c scheduler --
/app/linode-instance-scheduler <command>`.) The examples below leave out the `sudo`.

| Command | What it does |
|---|---|
| `migrate-start` | **One-time, only for a node whose OS is still on local disk.** Starts moving it onto Block Storage — creates the destination volume and boots the node into Rescue Mode. Prints one manual command for you to run. |
| `console-session-delete` | Delete a migration's Rescue Mode console session log (removed automatically after the retention period). See §6.6. |
| `rollback` / `backup-list` / `backup-delete` | Put the original system back from the backup `migrate-start --backup` kept, list backups, or delete one when you no longer need it (held for a grace period first, see `pending-deletions`). See §6.5. |
| `migrate-resume` | Finishes what `migrate-start` began, after you've run that one manual command. Boots the node from its new Block Storage volume and reserves its IP. |
| `migrate-copy` | Optional: runs the copy step for you in the Rescue Mode console, then `migrate-resume`. Only when a pre-migration backup is kept (§6.6). |
| `migrate-orphans` | Lists (or `--cleanup`s) destination volumes left behind by a `migrate-start --force` restart — see §6.4. You'll rarely need this. |
| `onboard` | Registers an already-running, already volume-based node with this tool by name. Pure capture — reads the node's current state, changes nothing on it. |
| `start` | Brings a stopped node back online — recreated identically at the same IP. With `--group-name`, starts every member of a group at once. See §8.13. |
| `stop` | Takes a running node offline — deletes the instance, keeps its data and IP. With `--group-name`, stops every member of a group at once. See §8.13. |
| `onboard-readiness` | Checks every instance on your account: ready to onboard, needs a migration first (with the volume size, its monthly cost and a rough copy time), or blocked and why. Read-only. `--json` for scripts, `--all` to include nodes already managed. See §7. |
| `migrate-wave` | Migrates several instances off local disk and onboards them, a few at a time: `--instance NAME=ID` (repeat), `--wave-size` (default 3). Each instance runs on its own; one failing never stops the others. See §6.7. |
| `pool-register` | Schedules an LKE node pool like an instance: stopping removes its nodes, starting brings them back. `pool-set-stop-mode` changes how it stops. See §8.16. |
| `set-mode` | Makes a node manual-only (`--manual`: the scheduler never starts or stops it) or schedulable again (`--auto`). See §8.13. |
| `api-token-create` / `api-token-list` / `api-token-revoke` | Manage API tokens for scripts calling the REST API. See §8.13. |
| `list` | Shows every node you've onboarded and its current status at a glance. |
| `status` | Shows the full captured record for one node. |
| `why` | Says in one line why a node (`--name`) is running or stopped right now, and when it changes next; with `--group-name`, the same for every member of a group. |
| `diagnose` | Works out why a node (`--name`) or group (`--group-name`) is in its state, or why an alert fired (`--text` with the pasted alert), from what was recorded: start/stop history and failures, activity, when the scheduler was stopped or standing by, setting changes, start order, holidays and Linode's own events. Names the cause with its evidence and what to do; changes nothing. |
| `changes` | The setting change history (90 days): who changed a schedule, group, membership, start order, holiday, wake policy, manual-only setting, hooks, budget, guardrail, address or SSH port, with the old and new value. |
| `history` | Shows the audit trail (create/delete events) for one node — "why was my instance down at 9am?" Works even for a node you've since `offboard`ed. |
| `savings` | A node's (`--name`) or group's (`--group-name`) configured savings and, for a node, what it actually saved over the last `--days` (default 7). |
| `activity` | What the scheduler, API and CLI did, filtered by node, group, level or text — the dashboard's Activity page. |
| `hook-history` | A node's recent hook runs: result, exit code and (`--output`) the output tail. |
| `reserve-ip` | Reserves an instance's current public IPv4 (onboarding needs it reserved). |
| `ssh-check` | Checks an instance's SSH port from this host; `--login` tries a real root login with this deployment's key (or a password from stdin with `--password-stdin`) and says whether the port, the host key or the credential failed. Changes nothing. |
| `backup-estimate` | What keeping a pre-migration backup of an instance would cost a month. |
| `schedule-set` | Sets (or replaces) one node's automatic start/stop schedule. See §8.5. |
| `schedule-show` | Shows one node's current schedule, if any. |
| `schedule-clear` | Removes one node's own schedule — it follows its group's schedule if it's in one, otherwise it goes back to manual-only control. |
| `group-create` | Creates a new, empty schedule group — give it a schedule afterward with `group-schedule-set`. See §8.6. |
| `group-schedule-set` | Sets (or replaces) a group's automatic start/stop schedule — applies to every current member. See §8.6. |
| `group-show` | Shows a group's schedule and its current members. |
| `group-list` | Lists every schedule group you've created. |
| `group-delete` | Permanently deletes a schedule group. Refuses if it still has members. |
| `group-depends` | Makes a group start only after one or more other groups are up and ready, and stop only after it is down (e.g. app servers after their database and cache). See §8.6. |
| `group-add` | Adds (or moves) a node into a schedule group. |
| `group-remove` | Removes a node from its group — asks what to do about its schedule if it doesn't have one of its own. See §8.6. |
| `poll` | Runs the scheduler — checks every node's individual AND group schedule and starts/stops it if due, and auto-reverts any expired manual override. Run it continuously (the normal way), or `--once` from cron. See §8.5/§8.6/§8.7. |
| `serve-api` | Runs the optional REST API server — the same capabilities as the CLI, over HTTP, with "Login with Linode" auth. See §8.8. |
| `extend` | Pushes an active manual-override auto-stop timer further out, or keeps a node (`--name`) or every member of a group (`--group-name`) running past today's scheduled stop. See §8.7. |
| `hooks-set` | Sets a node's (or a group's) pre-stop hook and/or post-start check — your own command, script path, or uploaded script, run on the node right before every stop and right after every start. See §8.12. |
| `hooks-show` | Shows a node's own hooks and the ones that actually apply to it (its own, or inherited from its group), or a group's hooks. |
| `hooks-clear` | Removes a node's own hooks (its group's then apply), or a group's hooks. |
| `hooks-run` | Runs a node's hook right now, without starting or stopping it — to test a hook, or re-run a failed post-start check after fixing the cause. |
| `clear-lock` | Admin escape hatch — forcibly clears a stuck in-progress operation. You should rarely need this: the scheduler clears a stale one by itself, and the dashboard has a **Clear lock** button. |
| `heal` | Compares every node with your account and fixes the local records where the account proves them wrong (an instance the records don't know about, a leftover from an unfinished start, a stale in-progress mark). The scheduler does this every few minutes. See §8.15. |
| `schedule-catch-up` | Lists nodes that aren't in the state their schedule wants (after an outage longer than the scheduler's one-hour catch-up window); `--apply` starts/stops them after you confirm. See §8.15. |
| `migrate-abandon` | Cancels an unfinished migration: the original instance boots from its own disk again and the copy's volume is deleted. See §6.4. |
| `env-backup` / `env-restore` | Your own encrypted copy of the settings (`.env`) and the deployment SSH key, encrypted to your age public key — only you can open it. See §8.10. |
| `backup-verify` | Restore-tests the backups now (the scheduler does it daily). See §8.15. |
| `doctor` | Checks that the database, your Linode account (tags, volumes, instances, VPC addresses), the Object Storage copies and the trusted host keys all agree. Read-only. Exit 0 consistent, 1 problems found, 2 a check couldn't run. `--json` for scripts. `--fix` shows the repairs for what it found and applies them after you confirm. See §8.15 and §8.17. |
| `explain` | Explains an error: paste its text (or `--file`, or `--name` for a node's last failure) and get its cause, read-only checks against this deployment, and what to do. See §8.17. |
| `scheduler-lease` | Which host is the active scheduler; `--take-over` makes this one active. See §8.15. |
| `host-watchdog` | Linode's Shutdown Watchdog on the scheduler's own host; `--enable` turns it on. See §8.15. |
| `alerts-check` / `alerts-test` | Show what needs attention right now (exit 1 if anything) / send a test alert. Alerts go to a webhook and/or email. See §8.15. |
| `reset-host-key` | Admin escape hatch — re-establishes SSH trust for a node after a genuine, confirmed key change. You should rarely need this either; see [§8](#8-day-to-day-usage) and the one-time migration note below. |
| `rebuild` | Disaster recovery — reconstructs your local registry from tags on your own Linode account, in case the machine running this tool (and its local records) is ever lost. You should rarely need this either. |
| `backup` | On-demand, whole-system backup — re-syncs every node's and group's Object Storage record, takes a full local/remote database snapshot, and saves the trusted host keys. Meant to be run on a schedule (cron/systemd timer). See §8.10. |
| `holiday-add` / `holiday-remove` / `holiday-list` | Holidays: dates on which scheduled starts are skipped (every node, one group or one node); stops still happen. See §8.14. |
| `holiday-settings` | Whether account-wide holidays apply to a group or a node (a node's own setting overrides its group's). See §8.14. |
| `budget-set` / `budget-clear` / `budget-list` | Monthly running-hour budgets for a group or node, and this month's use. See §8.18. |
| `guardrail-set` | Limits on manual starts and extensions: longest extension, most nodes running at once, quiet periods, the nightly safety net. See §8.18. |
| `approval-request` / `approval-list` / `approval-approve` / `approval-deny` / `approval-grant` | Ask for, and give, approval for a start or extension a budget or guardrail refused. See §8.18. |
| `hibernate` / `unhibernate` | Move a node stopped for a long time to Object Storage (its volumes are deleted after the grace period), and bring it back; choose the helper type, follow progress, resume or abandon. See §8.23. |
| `nb-membership` | Lists the VPC NodeBalancers a node is added back to after each start (a stop removes it from them), and `--forget` stops adding one back. See §8.24. |
| `wake-policy` / `wake-link` / `wake` / `wake-nodebalancer` | Wake on demand: keep a node or group stopped until someone needs it (or wake it outside its schedule), stop it again when idle; a secret wake link, or visitors through your NodeBalancer, can start it from a page. `wake-policy --name X` without `--mode` shows the current policy. See §8.21. |
| `schedule-suggest` | A schedule learned from the last 4 weeks of real use for a node or group; `--apply` applies it, `--undo` puts the previous one back. See §8.20. |
| `insight` | This deployment's own health — start/stop times, the Linode API, the scheduler, the host — with slow/high findings (cause and fix) and right-sizing hints. `--prometheus` for Prometheus. See §8.19. |
| `metrics-agent` | Optional server metrics on a node or group: installs node_exporter on the node (loopback only, read over SSH). See §8.19. |
| `users` / `team-*` | Who signed in to the dashboard, and the teams that give people a role. See §8.8. |
| `backup-config` | Shows where this tool backs itself up and how the last backup went; sets, tests or removes the Object Storage settings. See §8.10. |
| `restore` | On a replacement host: puts the newest (or a chosen, or a local) database snapshot in place, plus the trusted host keys. Run `rebuild` afterward. See §8.10. |
| `ssh-key-protect` / `ssh-key-restore` | The deployment SSH key is kept recoverable automatically (encrypted in the database and Object Storage, public half in your Linode profile, passphrase in your Linode account); `ssh-key-protect` checks it now, `ssh-key-restore` gets it back on a replacement host. See §8.10. (`ssh-key-backup` stores an extra copy under a passphrase you choose.) |
| `offboard` | Permanently decommission a stopped node — releases its reserved IP, removes it from tracking, and optionally deletes its volumes. For when you're actually done with a node, not just pausing it. The deletion waits out a grace period (7 days by default) during which the node can be recovered; `--now` skips it. |
| `pending-deletions` | Lists offboarded nodes and deleted backups waiting out their grace period; `--recover ID` brings one back, `--delete-now ID` deletes it at once. See §8. |
| `restore-item` | Puts back one node's schedule or hooks, or one group, as it was in a database snapshot — without restoring the whole database. See §8.10. |
| `release-backup` | Saves a copy of the installed release (code, built dashboard, every dependency) to Object Storage, so a new host can be installed without GitHub or PyPI. Done automatically when the code changes. See §8.10. |
| `bucket-protection` | Shows or turns on versioning for the backup bucket (turned on automatically), lists an object's versions, or brings an older one back. See §8.10. |
| `rotate-credentials` | Replaces a credential — the Linode API token, the Object Storage key, the dashboard OAuth secret, or the deployment SSH key on every node. See §8.15. |
| `recovery-drill` | Proves a new host could take over: a throwaway VM installs the release copy and the latest snapshot, checks everything would work, and is deleted. See §8.15. |
| `set-vpc-address` | Gives a stopped node's VPC interface a different address, used from its next start — for when another instance took its address while it was stopped. |
| `set-vlan-address` | Gives a stopped node's VLAN interface a different address, used from its next start — for when another instance on the VLAN uses the same address. |
| `set-public-ip` | Gives a stopped node a different reserved public IP (a free one, or `--reserve-new`), used from its next start. |
| `network-check` | Every address a node uses (public, VPC, VLAN), who holds each one right now, and a free replacement for any address someone else holds. |
| `set-ssh-port` | Records the port a node's SSH server listens on, if not 22; a running node is checked on the new port first. |

Everything in this table can also be done from the dashboard and the REST API, except what only
makes sense on the scheduler host itself (`poll`, `serve-api`, restoring the whole database, and
restoring or backing up keys and settings with secrets). Account-wide repairs — rebuild from
tags, self-heal now, leftover migration volumes, credential rotation, recovery drill, the release
copy, bucket versions, sending alerts now and forgetting a host key by address — are on the
dashboard's **Maintenance** page.
| `deregister` | Admin escape hatch — removes a node from local tracking only, with no changes to the real Linode instance, volumes, or reserved IP at all. For correcting a wrong or unsafe local record, not for decommissioning a real node (use `offboard` for that). |

> **Upgrading this tool on a fleet you already manage?** Read the one-time migration note in
> [§8](#8-day-to-day-usage) before your next `start`/`stop` — it takes one command per existing
> node and only needs to be done once.

Full detail on each below.

---

## 6. One-time migration onto Block Storage (Path B)

**Skip this whole section if the node you're onboarding was already built with its OS on a
Block Storage volume.** This is only needed for a node like the one built in §3 — local disk,
the way any ordinary Linode is created by default.

**Why this step exists at all**: this automation's entire mechanism depends on being able to
delete an instance and recreate it identically. A *local disk* is destroyed the instant its
instance is deleted — there'd be nothing to recreate from. A *Block Storage volume* is an
independent resource that survives deletion. So before this tool can manage a node, that node's
OS has to actually be living on a volume, not local disk.

This is automated end to end **except one step that genuinely can't be automated**: the actual
disk copy only runs inside Linode's Rescue Mode, which is only reachable through the interactive
Lish console — there's no way to run an arbitrary command there through the API. That one command
is the only manual step in this entire process.

### 6.0 How the original instance is treated: no-touch, clone or in place

`migrate-start --method` (and the dashboard's migration screen, and `migrate-wave --method`) chooses
what happens to the instance you're migrating:

| Method | The original instance | Downtime | What it costs while kept |
|---|---|---|---|
| **`no-touch`** (the default) | Never changed. Its disk is copied to a new volume, then it is powered off and a **new instance** with the same public IP, VPC/VLAN addresses, plan, kernel, label and data volumes boots from the copy. The original stays powered off as your fallback. | The copy (or only its short final pass with `--two-phase`) plus the new instance's first boot | The original instance at its plan price, until you delete it |
| `clone` | A powered-off native clone of it and its volumes is kept, then the original itself is migrated in place (same as `--backup`). | Cloning plus the copy | The clone and its cloned volumes |
| `snapshot` | A Linode Backups snapshot is taken while it runs, then a short second one once it is powered off; then it is migrated in place. Restored automatically if the check after migrating fails. | The second snapshot plus the copy | The Backups add-on while it is on (turned off again afterwards if it was off) |
| `in-place` | Migrated in place; nothing else is kept. | The copy | Nothing |

With **no-touch**, the original is renamed `<label>-original`, tagged as this name's pre-migration
backup and listed on the Backups page, with what it costs a month; its data volumes are not copied --
they move to the new instance. The migration summary says it plainly: the original is kept powered off
and is still billed until you delete it (`backup-delete --name X`, or Backups -> Delete). To go back,
`rollback --name X` (Backups -> Roll back) gives it back its public IP, VPC/VLAN addresses, label and
data volumes and boots it; the new instance is deleted and its volumes are kept. If the finish step
fails after the addresses have moved (the new instance doesn't boot from the copy, or can't be
reached), they go back to the original and it is booted again automatically; the copied volume is
kept, so you can fix the cause and run `migrate-resume` again. The original, its record and its tag
are kept in Object Storage and on the instance itself, so `rebuild` knows about it after a database
loss.

**After every migration, a check runs on the migrated system**: every filesystem its `/etc/fstab`
mounts at boot must be mounted. If it fails, a no-touch migration puts the original back (above), a
clone or snapshot migration is rolled back to the clone or snapshot automatically, and an in-place
migration reports what is missing (there is nothing to roll back to).

With **snapshot**, Linode Backups ends the way the original had it: if Backups was off, it is turned
off again once the migrated system passes its check -- this deletes the snapshot, and Linode then
won't let Backups be turned on again for that instance for 24 hours; if it was on, it stays on. The
snapshot belongs to the instance, so it is gone at the node's first scheduled stop (the instance is
deleted then). The migration summary, and each instance's line in a wave's summary, says what was
done with Backups.

Because nothing on the original changes, the assisted copy over Lish (§6.6) is available for
no-touch (and snapshot) without a clone, and waves (§6.7) use no-touch by default.

### 6.1 `migrate-start`

```
linode-instance-scheduler migrate-start --name app-1 --instance-id <your-instance-id>
```

- `--name` — a short name **you choose**, to refer to this node by from here on. Doesn't have to
  match its Cloud Manager label.
- `--instance-id` — the numeric Linode instance ID (visible in Cloud Manager, or in the
  instance's URL).

**What this actually does:**

1. Runs two hard pre-flight checks over SSH: that `cloud-init` is installed and new enough
   (23.3.1 or newer, or an older build that already reports Akamai's datasource, as some
   Linode images ship), and that its metadata datasource is Akamai's. Both are required for a
   later step in this tool to work correctly; it refuses to proceed if either is missing. It also
   refuses outright if your instance somehow has more than one boot config — Linode's API has no
   "which one is active" field, so this tool won't guess which one to read the current disk from
   (the same refusal `onboard`/`rebuild` have for the same reason).
2. Warns (but doesn't block) if it finds hand-configured networking outside the OS's normal
   Network Helper — e.g. a custom netplan file — and says, for each file, what happens to it on
   a recreate. This tool never edits or deletes such files; on every start it writes the
   instance's own addresses, gateway and DNS in its own files. For systemd-networkd, the first
   file in name order that matches an interface decides it: a file named before
   `05-eth<N>.network` (e.g. `00-eth0-custom.network`) keeps deciding that interface (fine as long
   as its addresses stay right; this tool keeps them the same unless it has to give a stopped node
   a new VPC address), a file named after it stops applying, and a file for other interfaces (a
   Docker bridge) is left alone. NetworkManager's ethernet connection profiles are replaced with
   ones carrying the same addresses.
3. Creates a new, appropriately-sized Block Storage volume to hold the migrated OS.
4. Boots your instance into Rescue Mode, with the original disk and the new volume attached.
5. Prints a single copy command for you to paste into the rescue console. The command finds the
   two disks itself — the new volume by its own Linode volume ID, the original disk by its
   size — so you never type device names.

**Output looks like this** (the copy command is shortened here; the real one is one long line):

```
YOUR TURN -- this is the one manual step in the whole process:
  1. In Cloud Manager, open instance <id> and click "Launch LISH Console".
  2. At the rescue shell (root@finnix), paste and run this one command. It finds the
     disks itself -- the new volume by its own ID, the original disk by its size
     (~20480MB) -- and refuses, copying nothing, if either is
     ambiguous. (Rescue Mode's device letters vary between systems, so don't type a
     dd command with fixed device names.)

       DST=$(readlink -f /dev/disk/by-id/scsi-0Linode_Volume_app-1-os-vol-1a2b3c4d); ... COPY_DONE ...

  3. Wait for COPY_DONE (this takes a few minutes). COPY_FAILED means the copy did not
     complete -- don't continue; run migrate-start --force to start over (add
     --two-phase again if you used it).
  4. If it printed 'Could not identify the disks', stop and check `lsblk`: the
     original disk is ~20480MB, the new volume ~25GB.
  5. Once it prints COPY_DONE, run:
       linode-instance-scheduler migrate-resume --name app-1
```

### 6.2 The one manual step

In Cloud Manager, open the instance and click **"Launch LISH Console."** You land at the rescue
shell (`root@finnix`). Paste the copy command `migrate-start` printed, exactly as printed.

**Why it doesn't use fixed device names.** Inside Rescue Mode, which `/dev/sdX` letter the
original disk and the new volume get varies between systems — the original disk can appear as
`/dev/sdg` rather than `/dev/sda`. A `dd` with the wrong letters copies in the wrong direction and
destroys the original disk. So the printed command identifies the disks itself: the new volume
through its `/dev/disk/by-id/scsi-0Linode_Volume_<label>` link, which names that exact volume,
and the original disk as the only other device matching its known size. If it can't identify
exactly one of each, it prints `Could not identify the disks unambiguously -- nothing copied`
plus the device list, and copies nothing.

**How it copies.** For an ext2/3/4 disk the command copies only the blocks the filesystem uses
(`e2image`, keeping the filesystem's UUID and label) and then checks the result read-only with
`e2fsck -fn` (if that copy or check fails, the command reports `COPY_FAILED`); for an xfs disk it
uses `xfs_copy` and checks with `xfs_repair -n`, and if either of those fails it copies the whole
disk with `dd` instead. Any other disk is copied whole with `dd` (`conv=fsync`). The command
prints how many bytes it has copied every few seconds as it goes (a two-phase migration, §6.6.1,
uses `rsync` and copies only the changes since the first pass). Everything is flushed to the volume
before the command reports done. It ends with one line:

- `COPY_DONE` — the copy finished and passed its check; run `migrate-resume`.
- `COPY_FAILED` — the copy did not complete, or its check failed (an I/O error, for example).
  Don't continue; run `migrate-start --force` to start over with a fresh volume (pass
  `--two-phase` again if you started with it — otherwise the retry copies the whole disk in
  Rescue Mode, with the full downtime).

The dashboard's migration page shows this same command to paste, and enables "finish" once the
pasted output shows `COPY_DONE`. A step-by-step whole-disk `dd` builder is still available there
behind a link.

For a typical node this takes a few minutes, roughly proportional to how much data is actually
on the disk.

### 6.3 `migrate-resume`

```
linode-instance-scheduler migrate-resume --name app-1
```

**What this actually does:**

1. Builds a real boot configuration pointing at the migrated volume, and reattaches any other
   Block Storage volumes (like your application's data volume) that were already on the node.
2. Shuts the node down (exiting Rescue Mode) and boots it from the new configuration.
3. Verifies it's genuinely booted from the *migrated* volume — not just that the API reports
   "running," and not just that SSH answers at all (a `dd` byte-for-byte copy means the guest's
   file contents look identical whether it's really running from the new volume or an old local
   disk still happens to be reachable; this checks the actual underlying block device instead).
   If that check doesn't match, this refuses and leaves the migration checkpointed exactly where
   it was — it will never report success against the wrong disk.
4. **Reserves the node's public IP in place** — the same address it already had, converted to a
   reserved IP so it's now permanently yours and will never change across future cycles.

When this finishes, your node is running exactly as before — same IP, same data — just with its
OS now on Block Storage instead of local disk. It's ready for the next step.

### 6.4 If something goes wrong mid-migration

**`migrate-resume` refuses with a phase error.** It only ever proceeds if `migrate-start`'s
checkpoint confirms the manual `dd` + `sync` step (§6.2) is actually done — building the real
boot config any earlier could boot into an empty or half-copied volume. If you see this and
you're certain `dd` + `sync` genuinely finished, or you want to abandon this attempt entirely,
re-run `migrate-start` with `--force` (below) rather than forcing `migrate-resume` past the check.

**Cancelling a migration.** Changed your mind, or the host running this tool was lost while a
migration waited for its copy step? Cancel it — the original instance is taken out of Rescue Mode
and boots from its own disk, which a migration never changes before `migrate-resume`, and the
destination volume (the copy) is deleted:

```
linode-instance-scheduler migrate-abandon --name app-1
```

It asks you to type the name (`--yes` skips that; `--no-boot` leaves the original powered off). A
kept pre-migration backup stays until you remove it (`backup-delete`). Once `migrate-resume` has
started changing the boot config, cancelling is refused — finish with `migrate-resume`, or
`rollback` if a backup was kept. If the original instance no longer exists, the destination volume
is kept instead of deleted (it may then hold the only copy of the system) — delete it yourself once
you're sure; if `migrate-resume` had already started, the record is cleared the same way so the
name can be migrated again. The migration page in the dashboard has **Cancel this migration**
for the same thing.

**`migrate-resume` itself crashes or times out partway through.** It's safe to just run it again
— it checkpoints its own progress (creating the boot config, shutting down, booting, verifying
device identity, reserving the IP) and reconciles against the node's actual live state before
repeating anything, so a retry never re-shuts-down an already-migrated instance or creates a
second boot config. If the reachable node turns out not to actually be booted from the migrated
volume (see step 3 above), the checkpoint stays put rather than advancing — re-running
`migrate-resume` will re-check the same thing, not silently move past it. That case needs a look
in Cloud Manager (Configs) before retrying, not a blind re-run.

**Re-running `migrate-start --force` for a name that already has an in-progress migration.**
This is safe — it starts a fresh attempt (a new destination volume, new Rescue Mode boot) rather
than resuming the old one. It re-runs every pre-flight check (instance running, `cloud-init`
version, networking) against the *replacement* attempt first, before touching anything about the
old one — if any of those fail (or if creating the replacement volume itself fails, e.g. a
capacity/quota issue), no replacement is created and the old attempt's tags and checkpoint are
left exactly as they were, so a failed `--force` never leaves you with contradictory local/remote
state. Only once the replacement volume genuinely exists and is durably checkpointed is the old
attempt's destination volume touched — it's **not deleted automatically**: its ID is preserved
and printed to you, and its Cloud Manager tags are switched from "active migration" to
"orphaned" at that point. (Every migration's destination volume is tagged this way in Cloud
Manager from the moment it's created, precisely so it's discoverable by tag even if the local
migration record (in `state/instances.db`) is lost mid-migration, not just once an attempt is abandoned or
completes — `migrate-orphans`' listing below merges in anything found this way even if the local
archive doesn't know about it.) The old ID is also recorded locally, in the database's list of orphaned migration attempts
(keyed by the node's name), once the *new* attempt's `migrate-resume` finally
succeeds — check on these anytime with:
```
linode-instance-scheduler migrate-orphans --name app-1
```
and delete them once you're sure they're no longer needed:
```
linode-instance-scheduler migrate-orphans --name app-1 --cleanup
```
(add `--volume-id <id>` to target just one, if there's more than one). Requires typing the name
to confirm, same as `offboard` (add `--yes` to skip the prompt, e.g. in a script). That list is
never cleaned up on its own otherwise — nothing scans it or reminds you it exists beyond this
command.

Like `rebuild` (above), `migrate-orphans` returns `0` only when nothing is wrong for the name(s)
checked, and non-zero if any real conflict or a scan failure is found — a conflict is still
printed as a `WARNING` either way, but if you're calling this from a script, cron job, or an
automated DR runbook, check the exit code rather than scraping the printed summary.

If you've lost the local migration record entirely (for example with the whole database) and `migrate-orphans` shows a volume still
tagged "active" for a migration you know is actually dead (confirmed by hand in Cloud Manager),
mark it orphaned so it becomes eligible for `--cleanup`:
```
linode-instance-scheduler migrate-orphans --name app-1 --volume-id <id> --mark-orphaned
```
This only ever retags — it never deletes anything on its own, and refuses if the volume isn't
actually tagged active for that name. Same confirmation prompt (type the name, or pass `--yes`)
as `--cleanup` above.

**A newly reserved or reused IP address won't accept an SSH connection during `migrate-start`,
even though the node is genuinely reachable.** Linode can reissue an address that this tool's own
trust records (`state/known_hosts`) still remember from a previous, unrelated node. Usually this
sorts itself out: `offboard` forgets a released address's key, and `migrate-start` and `onboard`
forget a stored key for an address no managed node uses before their first connection. If a
leftover key still blocks the connection (for example, an address a managed node is recorded
with), the node in question hasn't been onboarded yet, so `reset-host-key`'s usual `--name` form
doesn't apply here; use `--ip` instead:
```
linode-instance-scheduler reset-host-key --ip <the-address> --yes
```
Only do this after independently confirming (Cloud Manager, Lish console) that the address
genuinely belongs to the node you expect.

---

### 6.5 Keeping a backup of the original, and rolling back

The one step of a migration that can't be undone on its own is the copy: once the node boots from
its new volume, `migrate-resume` deletes the old local disk. If you want a guaranteed way back to
exactly the system you had, ask `migrate-start` to keep a backup first:

```
linode-instance-scheduler migrate-start --name web-1 --instance-id 12345678 --backup
```

Before anything is copied, the node is powered off and Linode clones it -- its disks and boot
configuration -- and each of its attached Block Storage volumes, using Linode's own clone feature.
The clone stays **powered off**, with its private (VPC/VLAN) interfaces removed so it can never take
or clash with an address while it waits; every setting needed to put the original back (plan,
region, label, tags, every interface and address, the public address, which volume sits in which
slot) is recorded locally and in Object Storage when configured, and the clone and its volumes
are tagged as this node's backup. The migration then carries on as usual.

**The backup is a billable resource until you delete it** -- a powered-off Linode still bills at
its plan's rate, and each cloned volume bills as Block Storage. Before creating it, `migrate-start`
shows the monthly cost from Linode's current price list (or a plain "billable resource" notice if
the price list can't be read) and asks you to confirm; `--yes` skips the question.

**Going back.** If anything about the migrated node is wrong, at any point -- mid-migration, after
`migrate-resume`, or after onboarding and any number of stop/start cycles:

```
linode-instance-scheduler rollback --name web-1
```

- The backup takes back the original public address (kept reserved), its VPC and VLAN addresses,
  its label, and its data volumes under their original labels, so the original `/etc/fstab`
  mounts them as before. It is then booted and checked over SSH.
- If the original instance still exists (a migration not finished yet), it is replaced: its public
  address is reserved first, its volumes are detached and kept, and the instance is deleted -- the
  backup holds the system it had before the migration. `rollback` refuses when the original still
  runs from its own disk with no migration in progress (a cancelled migration it has kept running
  since): replacing it would lose everything written after the backup. Copy what you need off it
  and delete it yourself first if you really want the backup back.
- If the node is onboarded, it must be stopped first (`stop`, or `stop --skip-precapture` if it
  can't be reached); `rollback` refuses while it is running. The scheduler then stops managing the
  name; the migrated volumes are kept, never deleted -- remove them when you no longer need them.
- `rollback` refuses when an original VPC address has since been taken by another instance; free
  it first.
- `--no-boot` restores everything but leaves the instance powered off.

The restored node is your original system, outside this tool. To schedule it again, migrate and
onboard it again (with a fresh backup if you like).

**When you're happy with the migration**, delete the backup so it stops billing:

```
linode-instance-scheduler backup-list
linode-instance-scheduler backup-delete --name web-1
```

`backup-delete` deletes the clone and its cloned volumes after a grace period (7 days by default,
`LIS_DELETION_GRACE_DAYS`): until then they stay as they are, marked pending deletion, and
`pending-deletions --recover ID` keeps the backup after all (see `offboard` in §8). The deletion
re-checks that each resource still carries this backup's tag. `backup-delete --now` deletes at once.
For a backup that has already been rolled back to, it only removes the record -- that instance is
your running system. In the dashboard, the same actions are
on the **Backups** page; the migration wizard offers "Keep a backup of the original first" on its
confirmation step.


### 6.6 Letting the tool run the copy (optional)

If a pre-migration backup is kept (§6.5), or the migration is two-phase (§6.6.1), the tool can
run the copy for you instead of you pasting
the command into the Lish console. It opens the instance's Rescue Mode console over Lish (Linode's
SSH console gateway, `lish-<region>.linode.com`), types exactly the command `migrate-start`
printed, follows the copy's progress, waits for `COPY_DONE`, and then runs `migrate-resume`:

```
linode-instance-scheduler migrate-copy --name web-1
```

**One-time setup.** The console is opened as your Linode user with this deployment's SSH key, so
that key has to be one of your profile's Lish keys. In Cloud Manager open **Profile → LISH Console
Settings**, allow key authentication, and add the deployment's public key (the `.pub` file next to
the key in `LINODE_SSH_KEY_PATH`). The tool only reads your profile to check this; it never
changes it. `--check` reports whether everything is in place and what's missing:

```
linode-instance-scheduler migrate-copy --name web-1 --check
```

**What it refuses.** Without a kept backup of this same instance, or a two-phase migration, it does
nothing (that is what makes the copy safe to automate: whatever happens, `rollback` or
`migrate-abandon` puts the original back). It
also refuses unless the migration is waiting for its copy. If the copy command can't identify the
disks, or the copy fails, nothing further happens: the migration keeps waiting for its copy, so you can
run `migrate-copy` again, do the copy yourself, or roll back. If the copy finishes but
`migrate-resume` fails, run `migrate-resume` again. `--no-resume` stops after the copy.

**Long copies.** A large local disk (several terabytes on the biggest plans) can take many hours
to copy. There is no limit on how long the copy may run: it only counts as failed if the copy makes no
progress for 45 minutes. The copy runs on the instance's own console, so it keeps going if the
Lish connection drops or the scheduler host restarts. The tool reconnects and keeps watching, and
if the copy finished while it was disconnected it reads the result from a status file the copy
command writes in the rescue system (`/tmp/lis-copy.status`). Running `migrate-copy` (or **Run the
copy for me**) again while a copy is running watches that copy instead of starting another one; if
it already finished, it goes straight on to `migrate-resume`. If Rescue Mode was restarted
mid-copy, the copy is gone and it says so: start it again.

The console session is recorded in `state/logs/lish-<name>.log`, cleaned of terminal control codes
and with each line timestamped (UTC), so `dd`'s progress reads as one line per update. In the
dashboard, the migration's copy step shows **Run the copy for me** when a backup is kept, or the
migration is no-touch or two-phase (the original is the fallback), and the key is registered, or
the setup steps above (with the key to paste) when it isn't. A no-touch or two-phase migration
started from the migration page runs the copy by itself as soon as it reaches that step with the
key registered, so it needs nothing typed into the console at all. While the copy
runs, **Watch the console session** opens it on the Logs page, updating live. After the copy it
finishes the migration and onboards the instance, the same as the manual path.

A session log is removed automatically once it has had no new output for
`ACTIVITY_LOG_RETENTION_DAYS` days (30 by default; the scheduler checks hourly). To remove one
sooner, select it on the Logs page and click **Delete session**, or run:

```bash
linode-instance-scheduler console-session-delete --name web-1
```

Deleting refuses (exit code 3) while anything holds the instance's lock, such as a copy that is
still running.

### 6.6.1 Shorter downtime: a two-phase copy

`migrate-start --two-phase` copies most of the data while the instance keeps running: the new
volume is attached to it, given an ext4 filesystem with the root filesystem's own UUID and label,
and filled with a file-by-file copy (`rsync`). Only then does the instance go into Rescue Mode,
where the copy step brings across just what changed since — usually seconds — and checks the
result. The instance is offline only for that and the reboots, however much data it has. A file
that changed is copied whole, and rsync needs that file's size free on the volume for the copy; if
it runs out of space (or its check fails), the copy step copies the used blocks of the whole
filesystem instead, which always fits but takes as long as a normal copy.

It needs an ext4 root filesystem directly on the boot disk (no partition table or LVM) and `rsync`
on the instance; otherwise `migrate-start` refuses before changing anything and the normal copy is
the way. If only `rsync` (or `e2fsprogs`) is missing, you can allow the tool to install it with the
instance's own package manager (apt, dnf, yum, zypper, apk or pacman): `--install-missing` on
`migrate-start` or `migrate-wave`, `install_missing` in the API, or **Install what's missing** next
to **Two-phase copy** in the dashboard (on the Onboard page, the migration page and the waves panel;
for waves it is asked once, upfront, for every instance). Nothing is installed without it. What it
installs is recorded, and cancelling the migration removes it again; after a finished migration it
stays on the migrated system. When you log in with root's password (or another key) instead of this
deployment's key, `migrate-start` adds this deployment's key to root's `authorized_keys` for the
rest of the migration; cancelling removes that too, so a cancelled migration leaves the original
exactly as it was. It can't be combined with `--backup` (the backup powers the instance off before the copy,
so the first pass couldn't run). The original disk is only ever read — in Rescue Mode it's mounted
read-only — so until `migrate-resume` has run, `migrate-abandon` puts the instance back on it
unchanged; for that reason the copy step can be run for you (§6.6) without a backup. If, in Rescue
Mode, the two disks don't look like a matching pair (different filesystem UUIDs, or no `rsync`),
the copy falls back to the used-block copy of the whole filesystem, which is always correct.
If the tool stops during the first pass (the host is lost, say), the new volume is left attached
to the running instance; `migrate-start --force` then refuses and points you at
`migrate-abandon`, which unmounts and detaches it and leaves the instance running — start again
after that.

In the dashboard, tick **Two-phase copy — short downtime** when onboarding tells you the instance is
still on local disk (next to **Migrate this instance onto Block Storage**), or on the migration
page's start screen; it can't be combined with keeping a clone or a Backups snapshot. For several
instances at once, use it with waves (§6.7).

### 6.7 Migrating many instances in waves

To bring a fleet across, migrate it a few instances at a time:

```
linode-instance-scheduler migrate-wave --wave-size 3 \
    --instance web-1=12345 --instance web-2=12346 --instance db-1=12347 \
    --instance cache-1=12348 --instance worker-1=12349
```

Each wave runs its instances in parallel, and the next wave starts when the previous one has
finished. For every instance it does what you'd do by hand: `migrate-start` with the chosen
`--method` (no-touch by default, §6.0), the copy (run for you over Lish, §6.6), `migrate-resume`,
then `onboard`. Each instance is powered off
only for its own copy. One instance failing — a refused pre-flight check, a copy that fails —
stops only that instance; the result lists every instance's outcome (`onboarded`, or `failed` with
the reason), and a failed instance can be finished by hand with the usual commands. Add
`--stop-after-failed-wave` to stop starting new waves after a failure.

Add `--two-phase` to copy each instance's files while it keeps running (§6.6.1): each is then down
only for the short final pass. It works with `--method no-touch` and `--method in-place`, not with
`clone` or `snapshot`; with in-place the copy still runs for you, since the original disk isn't
written until the copy's last pass. Instances whose disk isn't a plain ext4 filesystem are refused
by their own pre-flight check and listed as failed, while the others go on.

Leaving the originals untouched (no-touch) or keeping a clone is strongly recommended: that's what
makes the unattended copy safe to undo, and the copy step needs this deployment's key in your
profile's Lish keys (§6.6). With `--method in-place` and no `--two-phase`, each instance stops at its
copy command for you to run. Each kept
backup bills like its instance until you delete it with `backup-delete`. `onboard-readiness` (§7)
shows which instances need migrating, how big each volume will be and roughly how long each copy
takes. The same is available as `POST /migrations/waves` (`method`, `two_phase`) and in the
dashboard's waves panel on the Onboard page.

**Logging in to each instance.** A wave logs in to each instance as root with this deployment's SSH
key. For an instance that doesn't have the key yet, do one of these before starting:

- Add the key yourself. `migrate-wave --print-key-setup` (or **Show the command to add the key** in
  the waves panel) prints one command to run as root on each instance, in its console or over SSH. It
  adds the key to root's `authorized_keys` once, with the right permissions, and prints
  `LIS_KEY_READY`; running it again changes nothing.
- Give its root password. `migrate-wave --ask-passwords` asks for it for each instance that refuses
  the key; in the dashboard, type it in the instance's **root password** box; in the API, send
  `ssh_password` with that instance. Its migration logs in with the password once and adds this
  deployment's key, which every later step uses. The password is used for that wave only and is never
  stored; cancelling that migration removes the key again.

Check first with `migrate-wave --check-access --instance ...` (or **Check access** in the panel, or
`POST /migrations/waves/access-check`): it logs in to each instance the way the wave will and says
which are ready and why the others aren't (the port didn't answer, the host key differs, or the key or
password was refused). It changes nothing. The dashboard runs the same check when you start, and
starts nothing if any instance can't be logged in to; `--ask-passwords` does the same on the command
line.

**Good practice for waves:**

- Start with a pilot: a first wave of 1-2 instances that matter least; check them before the rest.
- A wave size of 3 to 5 is a good default; 10 is the most allowed. A bigger wave isn't faster for
  each instance, and if something is wrong it fails on more of them at once.
- There is no limit on the number of waves. They run one after another, and the total time is about
  the slowest copy in each wave, added up (the waves panel shows an estimate). Keep
  `--stop-after-failed-wave` on so a problem stops the remaining waves.
- Leave room on the account: with no-touch, each instance needs one more Linode while it migrates
  (the original stays, powered off and billed, until you delete it), and every instance gets a new
  Block Storage volume. Check that your account's instance and volume limits cover a whole wave.
- Use a maintenance window: each instance is offline from the start of its copy until it boots from
  its new volume (with `--two-phase`, only for the final pass).
- Keep an application and the database it needs in the same or back-to-back waves, and put similar
  instances together. Migrate the biggest disks last, or in a wave of their own.

## 7. Onboarding

**Which of my instances are ready?** Before onboarding one at a time, you can check the whole
account in one go:

```
linode-instance-scheduler onboard-readiness
```

It lists every instance this tool doesn't manage yet under **Ready to onboard** (it already boots
from a Block Storage volume; if its public IP isn't reserved yet, the entry says so -- reserve it
first, since onboarding refuses an unreserved one), **Need a migration first** (it boots from a local disk; shown with
the volume the migration creates, that volume's monthly cost and roughly how long the copy keeps
it offline), **Blocked** (and why, for example more than one boot configuration, or a region
without Block Storage) and **Skipped** (LKE nodes, and pre-migration backups this tool keeps). It
changes nothing. Copy times are estimates at about 250 MB/s; the cloud-init version and
hand-written network files can only be checked over SSH, so `migrate-start` checks those when you
run it. The dashboard's Onboard page has the same report ("Account readiness").

```
linode-instance-scheduler onboard --name app-1 --instance-id <your-instance-id>
```

If you just did a migration in §6, use the same `--name` you used there. If your node was
*already* on Block Storage (you skipped §6), pick any name you like.

**What this actually does — pure capture, nothing is changed on your node:**

- Confirms three things and refuses cleanly if any fail:
  - The node is currently **running** (needed to inspect it over SSH).
  - Its OS is genuinely on a Block Storage volume, not local disk.
  - Its public IP is **reserved**. (If you did §6, this is already true. If not, and this
    refuses, reserve the IP yourself first via Cloud Manager or the API, then re-run.) A node
    with **no public interface at all** — only VPC and/or VLAN interfaces — has no public IP to
    reserve and is onboarded without one; see "VPC-only and VLAN-only nodes" below.
- Reads and records: its network configuration, every attached data volume (including yours from
  §3.4, with its actual mount point and filesystem), the real contents of
  `/root/.ssh/authorized_keys`, its label and tags, its plan/size, any attached firewall, and its
  maintenance-policy/watchdog settings.

You'll see a summary printed of everything it captured — worth a quick glance to confirm it
looks right (in particular, that `data_volumes` shows at least 1 entry if you expected data to be
there).

From this point on, the node is under this tool's management by the name you gave it.

**SSH on another port.** If the node's SSH server listens on a port other than 22, pass it:

```
linode-instance-scheduler onboard --name app-1 --instance-id <id> --ssh-port 2222
```

`migrate-start` takes the same `--ssh-port`, and a node migrated that way is onboarded on that
port without passing it again. Every later connection to the node — the checks after a start,
the key re-read before a stop, hooks, SSH key rotation — uses it. To change it later (after
changing sshd's own port on the node), run:

```
linode-instance-scheduler set-ssh-port --name app-1 --port 2222
```

On a running node this first logs in on the new port and refuses if it can't, so a wrong port
can't lock the tool out. The port is kept with the node's recovery tags and Object Storage record,
so `rebuild` restores it. In the dashboard, the Onboard page's **SSH access** section has the port
field, and the instance page has an **SSH port** card.

**Testing access before you onboard.** In the dashboard's **SSH access** section, "Test
reachability" only checks that the port answers; "Test login" logs in as root once with the
credential you picked (this deployment's default key, a pasted private key, or root's password)
and tells you which part failed: the port didn't answer, the node's SSH host key differs from the
one this tool trusts for that address, or the node refused the credential. The test stores
nothing.

**VPC-only and VLAN-only nodes.** A node whose only interfaces are VPC and/or VLAN (no public
interface) is fully supported — onboard, stop, start, schedules, groups, hooks, `rebuild`, and
`offboard` all work the same way. What's different:

- **Your scheduler host must be able to reach the node's private address over SSH** (port 22,
  or the node's own SSH port):
  every start ends with a real SSH reachability check, and every stop reads the node's current
  SSH keys first. In practice that means running the scheduler inside the same VPC (or a VPC/
  network routed to it), or on the same VLAN for a VLAN-only node. When a node has both, the VPC
  address is used.
- **Its network identity is preserved exactly**: the same VPC/VLAN address on every cycle. On
  every recreate, a VPC-only node gets its default route via the VPC subnet's gateway (the
  subnet's first address) and the region's DNS servers, the same as Linode's own network
  configuration gives it. Whether it can reach the internet through that route depends on your
  VPC setup (for example a NAT gateway), exactly as before it was onboarded.
- **There's no reserved IP**, so `status` shows none and `offboard` has nothing to release.
- **Migrating one off local disk** (`migrate-start`/`migrate-resume`, or the dashboard's wizard)
  works the same way: pre-flight and verification connect over the VPC/VLAN address, the new boot
  config keeps the node's own VPC/VLAN interfaces (no public interface is added), and no IP is
  reserved. The dashboard's "Test reachability" check also tests that address. (Every migration
  keeps all of a node's interfaces — a public + VPC or public + VLAN node keeps both.)
- **Golden rule: give scheduled nodes their own private network.** Put the instances this tool
  schedules in their own VPC subnet and on their own VLAN label, and don't create other instances
  in that subnet or on that VLAN by hand. A stopped node's instance doesn't exist, so nothing holds
  its private address while it's stopped:
  - **VPC:** another instance in the subnet can be given the address. The tool notices before the
    next start and moves the node to a free address, so it still starts, but it comes back on a
    different address.
  - **VLAN:** Linode doesn't check VLAN addresses at all. An instance given the same VLAN address
    as a stopped node starts normally, and once both run, traffic goes to whichever answers first.
    Nothing fails, so Linode doesn't catch it; `network-check` (below) compares the VLAN addresses
    of every instance on that label and reports a duplicate, but a dedicated VLAN label is the
    real protection.
  - **Public IPs:** every managed node's public IP is reserved and stays with your account while
    the node is stopped, but a reserved IP that isn't in use can still be assigned to another
    instance by hand (Cloud Manager -> Reserved IPs -> Assign). The next start then refuses with
    the instance holding it, and `network-check` reports it in advance.

  If a subnet has to be shared, give every other instance an explicit address from the bottom of
  the range (the tool moves nodes to addresses at the top). Onboarding warns when a node's subnet
  or VLAN already holds instances this tool doesn't manage, and
  `linode-instance-scheduler status --name db-1 --check-network` runs the same check at any time.
  The scheduler's own host is expected in the subnet (it has to reach the nodes) and isn't counted.
- **Checking every address a node uses.** `linode-instance-scheduler network-check --name db-1`
  (or **Check** on the instance page's **Network addresses** card) lists the node's public IP,
  each VPC address and each VLAN address, and for each one whether another instance holds it
  right now ("this node's address is held by web-3 right now"), plus other instances in the same
  subnet or VLAN that this tool doesn't manage. A public IP that isn't reserved is flagged too: it
  would change at the node's next stop (reserve it with `reserve-ip` while the node runs). It exits
  1 when an address is held or unreserved. For a stopped node, each held address comes with a
  replacement:

  ```
  linode-instance-scheduler set-vpc-address --name db-1 --address 10.24.1.250
  linode-instance-scheduler set-vlan-address --name db-1 --address 192.168.5.250
  linode-instance-scheduler set-public-ip --name db-1 --address 172.236.10.20   # a free reserved IP
  linode-instance-scheduler set-public-ip --name db-1 --reserve-new             # reserve a new one
  ```

  The card does the same with **Use a new address**: a VPC or VLAN address comes pre-filled with a
  free one (editable), and a public IP is chosen from your reserved IPs in the node's region that
  no instance holds and no other node is recorded with, or **Reserve a new IP and use it**. Each
  takes effect at the node's next start. `set-vlan-address` refuses an address outside the VLAN
  interface's network or one another instance on that VLAN uses; `set-public-ip` refuses an
  address that isn't reserved, is in another region, or is assigned to an instance. A new public IP
  carries the node's recovery tags and trusted SSH host key with it; update DNS records and
  allow-lists that point at the old address. The old address stays reserved on your account (and
  billing) until you release it in Cloud Manager.
- **A stopped node's VPC address can be taken — and is reclaimed automatically.** While a node
  is stopped its instance doesn't exist, so Linode treats its VPC address as unused, and an
  instance created in that subnet can be given it (Linode hands out the lowest free address).
  Before every start — scheduled, manual or API — the tool checks the node's recorded VPC
  addresses against what is actually in use. If another instance holds one, the node is moved to
  a free address in the same subnet (taken from the top of the subnet, which new instances are
  least likely to get) and started there, with nothing for anyone to run; the start's output and
  the poller's log say which address it moved from and to, so you can update anything that
  reaches it by address. Its trusted SSH host key moves with it, so the usual strict host-key
  check still applies. The same move happens earlier when you migrate or onboard the new instance
  that was given the address: before connecting to it, `migrate-start` and `onboard` move the
  stopped node off that address (with its host key), so the new instance isn't checked against
  the stopped node's key. A node that isn't stopped, or is busy, is left alone with a warning. To
  choose the new address yourself instead, move the stopped node first:

  ```
  linode-instance-scheduler set-vpc-address --name db-1 --address 10.24.1.40
  ```

  (or "Change" on the node's **VPC address** card in the dashboard). It refuses an address outside
  the subnet, the subnet's gateway, one another managed node is recorded with, or one a running
  instance holds. Onboarding refuses an instance whose VPC address is already recorded for another
  managed node. To keep addresses stable, give other instances in that subnet their own explicit
  addresses, or keep scheduled nodes in a range nothing else is assigned from. (While a node is
  running — and during a migration — its address is held and can't be taken.)
- **SSH host keys on private addresses.** A node reached over a VPC/VLAN address with no host key
  on record for that address — after an automatic move, or if the tool's `known_hosts` file was
  lost — accepts the node's key on first contact at its next start, as onboarding does. A key
  that differs from one already on record is still refused. Nodes reached over a public reserved
  IP always use the strict check.
- **VPC 1:1 NAT** (a public address mapped onto the VPC interface instead of a separate public
  interface) is supported under both networking models. The guest is set up exactly like a
  VPC-only node, and the scheduler reaches it over its VPC address. Linode releases an ordinary
  NAT address when the instance is deleted, so the tool **reserves** it, the same way it
  reserves a public interface's address: when `migrate-resume` finishes, at onboarding, before
  every stop, and after a `rollback`. A reserved NAT address is reused on every start. If
  reserving fails, you get a warning and it is retried before the next stop; should a stop still
  go ahead without it, the next start gets a new public address and says which. Under the older (legacy config) model the NAT address is the instance's own public
  IPv4, so only one NAT address per node is supported; a node mapping two is refused at
  onboarding.
- **A reserved public IP that has since been assigned to another instance** (or released from
  the account) can't be given back to a stopped node, so its next start fails straight away with
  a message saying so, before anything is created. Unassign the address from the other instance
  in Cloud Manager (Networking -> Reserved IPs), then start the node again; a scheduled start is
  retried on every poller tick until its catch-up window ends.
- A **VLAN-only** node gets no default route or DNS from the tool — a VLAN has no gateway — so
  anything it needs beyond its own VLAN must come from its own configuration. Linode's metadata
  service isn't reachable from an instance whose only interface is a VLAN, so a recreated
  VLAN-only node runs from the network settings already on its disk (its VLAN address never
  changes), and onboarding writes one cloud-init setting on the node,
  `/etc/cloud/cloud.cfg.d/99-linode-instance-scheduler.cfg` (`ssh_deletekeys: false`), so its SSH
  host keys survive each recreate. The scheduler must be on the same VLAN to reach it.

**If the node has a VPC interface, you may need `--vpc-id`.** For the newer `linode` interface
model, the VPC's ID is already part of what's captured — nothing extra needed. For the older
`legacy_config` model, the API doesn't expose `vpc_id` directly on the interface, so this tool
first tries to auto-discover it by scanning your account's VPCs for the one containing the
node's subnet. That works as long as the VPC is visible to this tool's API token. If it can't
find it (a scoping issue, or the VPC belongs to a different account/project), `onboard` refuses
with a clear error and you supply it explicitly:

```
linode-instance-scheduler onboard --name app-1 --instance-id <id> --vpc-id <vpc-id>
```

(The VPC ID is visible in Cloud Manager under VPCs, or via the API.) You'll only ever need this
for a `legacy_config` node with a VPC interface where auto-discovery fails — most nodes never
need this flag at all.

**Re-onboarding an already-managed name with `--force`** (e.g. to re-capture after manual
changes) is safe and routine when it's the *same* underlying instance. If it resolves to a
**different** instance, volume, IP, or region than what's currently on record, you'll see an
explicit diff (old value → new value) and have to type the name to confirm — this exists
specifically to catch a mistyped `--instance-id` before it silently redirects a name you already
trust to unrelated infrastructure. If that different instance's own OS volume, data volume, or
reserved IP is *already* tagged in Cloud Manager under a **different** name (e.g. you meant to
onboard a fresh node but typed an `--instance-id` that's already managed elsewhere), onboarding is
refused outright, naming the conflicting node — there's no override flag or confirmation prompt
that lets you push through this one. That's deliberate: overriding it would leave a resource
claimed by two names at once, which is exactly what `rebuild`'s disaster-recovery scan is designed
to detect and refuse to guess about (for *either* name). If you genuinely want to repoint a name at
infrastructure another name currently owns, offboard the other name first (`offboard --name
<other-name>` without `--delete-volumes`, see §8). Its tags stay on the resources while its
offboarding is pending deletion, and are removed from the volumes it keeps when that is carried
out (after the grace period, or at once with `pending-deletions --delete-now ID`); then re-run this
onboard. `--yes` still skips the identity-diff confirmation
above; it does not, and cannot, skip this refusal. If a `--force` re-onboard moves this name to a
**different reserved IP** than it had before (confirmed via either prompt), this tool's own
cached SSH trust for the *old* address is also cleared automatically — the same cleanup
`offboard` already does when it releases an IP — so a future reissue of that old address to an
unrelated node won't need a manual `reset-host-key --ip` first.

---

## 8. Day-to-day usage

> **One-time migration note — read this if a node in your fleet was onboarded with an older
> version of this tool, before SSH host-key verification existed.** `start`/`stop` verify a
> node's SSH host key against a record this tool keeps itself (`state/known_hosts`) — strictly,
> by default, since a steady-state recreate is guaranteed to present the *same* key every time
> (see `start` below). For a node reached on a public reserved IP, that record has to exist
> first: with nothing recorded yet, its next `start` or `stop` fails with a `SECURITY WARNING` —
> not because anything is actually wrong, but because there was nothing to compare against yet.
> (`rebuild` records the key it finds, and a node reached only over a private VPC/VLAN address
> with no stored key is trusted on first contact, so neither needs this.)
> **Fix, once per such node with nothing recorded, before your next `start`/`stop`:**
> ```
> linode-instance-scheduler reset-host-key --name app-1 --yes
> ```
> This connects once, trusts whatever key the node currently presents, and records it. Do this
> for every node in this situation — after that, every future `start`/`stop` verifies against it
> automatically and you'll never need to touch this again unless you genuinely rotate a host's
> key on purpose (see `reset-host-key` below).

**Different SSH ports:** give an instance whose SSH server isn't on port 22 its port after the
ID, e.g. `--instance web1=12345 --instance db1=12346:2222`. Each instance's port is used for its
own migration and onboarding, and recorded for every later connection.

**From the dashboard:** on the Onboard page, "Check the account" lists the instances that need a
migration; under **Migrate several at once, in waves** tick the ones to migrate, check the
suggested names (and each one's SSH port if it isn't 22), pick a wave size, and start. The result lists each instance's outcome. Without
backups, each one stops at its copy step, with a link to its migration page.

### `stop` — take a node offline

```
linode-instance-scheduler stop --name app-1
```

Prompts for confirmation (add `--yes` to skip it, e.g. in a script). Re-captures everything
first — network config, data volumes, label/tags, and instance settings — in case anything
changed in Cloud Manager since the last cycle, so an out-of-band change (a rename, a re-tag, a
firewall swap) is never silently lost. Then refreshes this tool's own disaster-recovery tags on
your volumes/IP before deleting the Linode instance. **The data volume(s) and the reserved IP
are never touched — only the compute instance is deleted.**

If that tag refresh fails, `stop` refuses rather than deleting anyway — the live instance is the
best remaining source of truth for disaster recovery, and deleting it while that mapping is
known to be wrong or unconfirmed would make things worse, not better. A genuine ownership
conflict (these resources are tagged for a different name) or a verification failure always
refuses; a plain transient API error also refuses by default, but can be overridden with
`--force` if you know the tagging service had a momentary blip and need to stop the instance
anyway:

```
linode-instance-scheduler stop --name app-1 --force
```

**If the instance is already powered off** (for example an earlier stop shut it down but Linode
took longer than the tool waits to confirm it), `stop` doesn't need SSH at all: it keeps the
node's last-known `authorized_keys`, skips the pre-stop hook and deletes the instance as usual,
so a retried scheduled stop finishes by itself.

**If the node itself is SSH-unreachable** (a guest firewall rule, a crashed `sshd`, a wedged
network stack), the one re-capture step that needs SSH (reading the node's `authorized_keys`)
will retry for a while and then `stop` will refuse, with nothing deleted. Network config, data
volumes and instance settings are read from the Linode API and don't need SSH. If you've independently confirmed (Cloud Manager, Lish console) that the
node is genuinely unreachable and you just need to stop billing, add `--skip-precapture`:

```
linode-instance-scheduler stop --name app-1 --skip-precapture
```

This keeps the last-known `authorized_keys` list in the registry instead of reading it fresh over
SSH (and skips the pre-stop hook). Data volumes and everything else are still read from the API
as usual, and the node's own `authorized_keys` file on its disk is what the recreated instance
uses, so nothing about its access changes. Only use it when you've confirmed the unreachability yourself —
see [Troubleshooting](#11-troubleshooting) for the full recovery sequence if the instance ends up
deleted out of band (e.g. via Cloud Manager) instead.

### `start` — bring it back online

```
linode-instance-scheduler start --name app-1
```

Recreates the instance from scratch: same plan, same reserved IP, same volumes reattached at the
same device slots, same label/tags/firewall/settings, same SSH host key (no "remote host
identity changed" warning). Waits for it to actually respond over SSH — not just for the API to
report "running" — before telling you it's up, and **actively verifies the host key matches what
this tool already has on record for that node**, not just that *some* key was accepted. If it
genuinely doesn't match, `start` refuses with a `SECURITY WARNING` rather than silently
continuing — see the one-time migration note above if you're seeing this on a node you know is
fine, or `reset-host-key` below if a key change is real and confirmed.

### `list` — see everything at a glance

```
linode-instance-scheduler list
```

```
app-1: running  ip=172.x.x.x  vpc=10.0.0.5  region=in-bom-2  linode_id=123456
db-1:  stopped  ip=None  vpc=10.0.0.6  vlan=192.168.5.2  region=in-bom-2  linode_id=None
```

`ip` is the reserved public IP (`None` for a node with no public interface); every VPC and VLAN
address the node uses follows it, and `nat` for a public address mapped by VPC 1:1 NAT. The
addresses come from the node's record, so they show the same whether it is running or stopped.
`list --json` and `status` carry them as `ip_addresses` (a list of `{kind, address, ...}`), and the
dashboard shows them in the **IP addresses** column of the instances list and on the instance
page's Status card.

(`in-bom-2` here is just an example, not a required region — your instances' regions are
captured from wherever they actually live. Any Block-Storage-capable core region is supported;
distributed compute regions aren't, since this tool requires the OS to live on a Block Storage
volume.)

### `status` — full detail on one node

```
linode-instance-scheduler status --name app-1
```

Prints the complete captured record for that node — useful for confirming exactly what will be
reproduced on the next `start`, or for debugging.

### `why` — why is it running (or stopped) right now?

```
linode-instance-scheduler why --name app-1
linode-instance-scheduler why --group-name app
```

```
Running: inside group 'app''s schedule hours (Asia/Kolkata); it stops at 18:00 Asia/Kolkata today.
  Started by the scheduler at 09:00 Asia/Kolkata today.
```

Names the one rule deciding the node's state right now, worked out from the same rules the scheduler
itself uses: its schedule (its own or its group's) and whether it is inside the hours, a holiday, an
extension or a manual start's auto-stop, wake on demand (with the earliest idle stop time), a start
waiting on a group it depends on, a stop waiting on the groups that depend on it, manual-only,
hibernation, or a state needing attention. Supporting lines say who last started or stopped it and
whether its last start or stop failed. For a group, the first line rolls the members up ("3 of 4
running -- 3 inside the schedule, 1 stopped by hand") and one line per member follows. `--json`
prints it for scripts. `status` also prints the one-line answer (to stderr). The dashboard shows it
as **Why** on the instance page, and as **Now** on a group page; the API is
`GET /instances/{name}/why` and `GET /groups/{name}/why`; in Slack or Teams, `why <node>`.

### `history` — "why was my instance down at 9am?"

```
linode-instance-scheduler history --name app-1
```

```
2026-08-25T09:03:11+00:00  delete  success  triggered_by=manual
2026-08-25T09:00:02+00:00  create  success  triggered_by=manual
```

Every real `stop`/`start` attempt that genuinely reaches the point of creating or deleting the
underlying Linode instance gets recorded here — newest first, with a `--limit N` flag (default
50) if you only want the most recent handful. A no-op call (the node was already in the state
you asked for, or you declined a confirmation prompt) is never recorded, since nothing was
actually attempted. This works even for a node you've since `offboard`ed — the history is kept
independently of whether the node is still tracked, so a past node's story isn't lost the moment
it's decommissioned.

### `clear-lock` — admin escape hatch

Every `start`/`stop` holds a lock on that node for the duration of the operation, so two
operations can never run against the same node at the same time (e.g. two people both running
`start` for the same node at once). If something crashes mid-operation and leaves a node showing
as locked when nothing is actually still running:

```
linode-instance-scheduler clear-lock --name app-1
```

You'll get a warning first and a `[y/N]` prompt — **only confirm after independently checking in
Cloud Manager that nothing is actually still in flight** for that node. Add `--yes` to skip the
prompt (e.g. in a script), but only once you're scripting around a case you've already verified is
safe. (If another `start`/`stop`/`rebuild`
process for this same node is genuinely still running right now, `clear-lock` refuses outright
with a clear error instead of racing it — this only ever matters for a lock left behind by
something that actually crashed.) The scheduler also clears such a mark by itself within a few
minutes, and the instance's dashboard page shows a **Clear lock** button while it's set.

### `reset-host-key` — admin escape hatch for a genuine SSH key change

Every `start`/`stop` verifies a node's SSH host key against what this tool already has on record
for it (see the one-time migration note above) — and refuses if it doesn't match, rather than
silently trusting whatever's presented. That's deliberate: it's exactly the check that would
catch a real problem (wrong disk, a misconfigured DNS/routing issue, or worse). This command is
the *only* way to move past a genuine mismatch:

```
linode-instance-scheduler reset-host-key --name app-1 --yes
```

**Only use this after independently confirming the new key is legitimate** — e.g. via Cloud
Manager's Lish console, not by trusting the same SSH connection that might itself be the problem.
Real reasons you'd need this: you intentionally replaced a node's disk outside this tool, or
you're migrating an already-onboarded node to a tool version with this check for the first time
(the one-time migration note above). It's not something you should need routinely.

For a node that **isn't onboarded yet** — most commonly a reserved IP Linode reused that this
tool's own records still remember from a different, earlier node (see §6.4) — use `--ip` instead
of `--name`, since there's no registry record to look the address up from yet:
```
linode-instance-scheduler reset-host-key --ip 172.x.x.x --yes
```
Both forms behave the same otherwise: confirm by typing the name/address, then this tool tries a
fresh connection before actually forgetting the old key — if that connection fails, the old
trust is restored automatically rather than left blank, so a failed attempt never leaves the node
in a *worse* state than before you ran the command.

### `rebuild` — recovering from a lost local machine

Your local registry (the file this tool keeps track of your nodes in) lives only on whatever
machine you run it from. If that machine is ever lost entirely — hard drive failure, an
accidentally wiped laptop, whatever — this command gets you back.

**How it works**: every time you `onboard`, `start`, or `stop` a node, this tool tags that node's OS
volume, data volume(s), and reserved IP directly in your Linode account with the mapping
("these resources belong to `app-1`"). That's the one piece of information that isn't
recoverable from Linode's API on its own — everything else about a node (its network config,
tags, size, etc.) can always be re-read from the live instance. So instead of backing up the
whole local file somewhere, the one thing that actually matters is written directly onto your
own Linode resources.

```
linode-instance-scheduler rebuild
```

This scans your account for those tags and rebuilds your local registry from them — no backup
file needed anywhere. Two outcomes, depending on whether each node happened to be running or
stopped at the moment you lost your local machine:

- **Node was running** — full recovery. Everything is re-read live, exactly like a fresh
  `onboard` — including the same refusal `onboard` has if the instance somehow has more than one
  boot config (Linode's API has no "which one is active" field, so this tool won't guess): that
  one name is skipped and reported, not silently recovered against the wrong config.
- **Node was stopped** — the tags say which volumes and which IP are yours, and the rest of what a
  `start` needs (network config, SSH access) comes from the node's own recovery tags for a simple
  single-interface node, or from its Object Storage record when backups are configured (§8.10).
  With that, the node is fully recovered as `stopped` and starts normally. If neither is enough,
  the tool won't guess: the node is marked `needs_manual_recovery`; boot it once manually via Cloud
  Manager from the recovered `os_volume_id`, then run `onboard --force` for it to fully restore
  management.

If the tags for a name turn out ambiguous or incomplete for any reason (e.g. a stale tag left on
an old resource, or resources for one name somehow spanning more than one region — neither
should happen in normal use), that node is skipped and reported rather than guessed at; the
warning tells you exactly what to check in Cloud Manager before re-running.

**If the node had a schedule set (§8.5), or belonged to a group (§8.6), `rebuild` recovers those
too** — not just the node itself. Both are mirrored onto the node's OS volume as tags the same
way its identity is, specifically so they survive this exact scenario; nothing extra to do, they
come back automatically alongside the node in the same `rebuild` run. If the group itself was
lost too (not just this one node's own record), it's recreated from this node's own tags —
another member of the same group recovered in the same `rebuild` run just rejoins it, no
duplicate group.

**Exit code**: `rebuild` returns non-zero if any node it found failed, was skipped as ambiguous/
incomplete, or had a tag-derived name that failed validation — or if it found contradictory tags
on a resource outright. It returns `0` only when every node it found was cleanly recovered (fully
or partially). If you're calling this from a script, cron job, or an automated DR runbook, check
the exit code rather than scraping the printed summary.

**If a recovered, running node has a VPC interface under the older `legacy_config` model**, add
`--vpc-id` — the same auto-discover-then-fallback flag `onboard` uses (§7), for the same reason
(that model doesn't expose `vpc_id` directly, so it's inferred by scanning your account's VPCs).
It applies to every node recovered in that one `rebuild` run, not just one:

```
linode-instance-scheduler rebuild --vpc-id <vpc-id>
```

Only needed if auto-discovery can't find the recovered node's VPC (a scoping issue, or the VPC
belongs to a different account/project) — most runs never need it. If you skip it and a node
genuinely needs it, that node comes back missing
`vpc_prefix`, and its very next `start` fails with a `vpc_prefix is required...` error — that's
your signal to come back here and re-run with `--vpc-id`. Note that `rebuild` has no `--name`
flag — it always scans your whole account — so fixing just that one node still means re-running
`rebuild --vpc-id <vpc-id> --force` for the whole account again; `--force` is required since it'll
otherwise skip every name already in your local registry (see
[Troubleshooting](#11-troubleshooting) for `--force`'s exact behavior).

You'll rarely need this — it exists purely as a safety net for the worst case.

### `offboard` — permanently decommissioning a node

Everything above is about pausing a node and bringing it back identical. `offboard` is
different — it's for when you're genuinely done with a node for good, not just taking it
offline until next time.

```
linode-instance-scheduler offboard --name app-1
```

**Requires the node to already be stopped** — `stop --name app-1` first if it's still
running. Offboarding refuses cleanly on a running node rather than doing something surprising
with a live instance.

By default, this:
- **Releases the reserved IP** back to Linode's pool, permanently. If you ever re-onboard a
  similar node later, it gets a brand-new address — this exact one is gone for good.
- **Leaves the OS and data volumes intact** — nothing about your actual data is touched — but
  removes this tool's internal tracking tags from them, so a future `rebuild` can never mistake
  them for a node that still needs recovering.
- **Removes the node from your local registry.**

You'll be asked to type the node's name to confirm before anything happens — not something a
stray `y` at a prompt should be able to trigger by accident.

**Nothing is deleted at once.** The node leaves management immediately, but its reserved IP and
volumes are held for a grace period — 7 days by default (`LIS_DELETION_GRACE_DAYS`; `0` deletes at
once). During that time every resource keeps all of its tags (ownership, schedule, group, hooks)
plus three more marking it pending: `lis-pending-delete:<date>` (or `lis-pending-untag:<date>` on a
volume that will be kept) and `lis-pending-of:<name>`. When it's due, the active scheduler does the
deletion, re-checking each resource first: anything no longer marked, now attached or assigned to an
instance, or claimed by another name is left alone. To skip the grace period:

```
linode-instance-scheduler offboard --name app-1 --now
```

**To also permanently delete the volumes**, not just release the IP:

```
linode-instance-scheduler offboard --name app-1 --delete-volumes
```

This destroys the actual data on those volumes — irreversibly. Only use it once you're certain
you'll never need that data again. The default (no `--delete-volumes`) is deliberately the
safer choice: you can always delete volumes manually later via Cloud Manager once you're sure,
but you can't undo a deletion.

### `pending-deletions` — changing your mind after `offboard` or `backup-delete`

```
linode-instance-scheduler pending-deletions                  # what's waiting, and when it's due
linode-instance-scheduler pending-deletions --all            # also ones carried out or recovered
linode-instance-scheduler pending-deletions --recover 4      # cancel it
linode-instance-scheduler pending-deletions --delete-now 4   # delete it now (asks you to type the name)
```

Recovering an offboarded node brings it back **stopped**, exactly as it was: its schedule, group
membership, hooks, manual-only mode and holiday setting. `start` it as usual. Recovering a backup
simply keeps it again. `--delete-now` can't be undone; `--yes` skips typing the name. The
dashboard's **Pending deletion** page does the same, and the notice shown after offboarding links to
it. Over the API: `GET /pending-deletions`, `POST /pending-deletions/{id}/recover` and
`POST /pending-deletions/{id}/delete-now`.

While a name is pending deletion, `onboard` refuses that name — recover it, delete it now, or use
another name.
A pending node survives the loss of the scheduler's database: `rebuild` restores it as a pending
deletion (from Object Storage, or from its tags alone), never as a live node. An alert is sent if a
pending deletion is overdue or keeps failing.

### `deregister` — admin escape hatch for a wrong or unsafe local record

This is different from `offboard` above, even though both remove a node from your local
registry — the important difference is what each one touches on the Linode side:

|                          | `offboard`                                     | `deregister`                          |
|--------------------------|-------------------------------------------------|----------------------------------------|
| Requires the node stopped | Yes                                             | No — works in any status              |
| Releases the reserved IP | Yes, always                                     | No — never touches it                 |
| Can delete volumes       | Optionally (`--delete-volumes`)                 | No — never touches them               |
| Removes management tags  | Yes                                              | No — leaves everything as-is          |
| Removes local registry entry | Yes                                          | Yes                                    |
| Use it for               | Genuinely decommissioning a node for good        | The *record itself* is wrong or unsafe |

Use `deregister` when the problem is with this tool's own bookkeeping, not with the actual
node — the node itself should be left completely alone:

```
linode-instance-scheduler deregister --name test-node-1 --yes
```

The clearest real example: a node onboarded before it had actually been migrated onto Block
Storage (its OS still on local disk). This tool's own registry doesn't distinguish that
correctly in every version, and if you ever ran `stop` on a node like that, the delete would be
real and unrecoverable — a local disk doesn't survive instance deletion the way a Block Storage
volume does. If you ever suspect a node was onboarded incorrectly, `deregister` it, verify the
node itself is untouched (check Cloud Manager — it will be, since this command never talks to
the Linode API at all), fix the actual problem (e.g. run the Path B migration properly, see §6),
then `onboard` it again.

You'll be asked to type the node's name to confirm, same as every other one-way action here.
`--yes` skips the prompt.

### 8.5 Scheduling — automate start/stop

**This is only individual, per-instance scheduling** — one node, one schedule. There's no group
scheduling yet (see the "Known limitations" note below).

**Set a schedule**, e.g. weekdays 9am–6pm India time:

```
linode-instance-scheduler schedule-set --name app-1 --timezone Asia/Kolkata \
  --days mon,tue,wed,thu,fri --start-time 09:00 --stop-time 18:00
```

`--timezone` is any IANA name (`Asia/Kolkata`, `US/Pacific`, `UTC`, ...) — the schedule's own
day/time rules are resolved in that timezone, DST included, not UTC. `--days` is comma-separated
3-letter days (`mon`..`sun`). **Overnight windows work too**: a `--stop-time` earlier than
`--start-time` stops the *next* day — `--days mon,tue,wed,thu,fri --start-time 22:00
--stop-time 06:00` runs each weeknight from 22:00 until 06:00 the following morning (`--days`
names the day each window *starts*). Start and stop must differ.

Need more than one rule (e.g. different hours on weekends)? Pass a full JSON array instead:

```
linode-instance-scheduler schedule-set --name app-1 --timezone Asia/Kolkata \
  --rules-json '[{"days_of_week":["mon","tue","wed","thu","fri"],"start_time":"09:00","stop_time":"18:00"},
                 {"days_of_week":["sat","sun"],"start_time":"10:00","stop_time":"14:00"}]'
```

**View or remove it:**

```
linode-instance-scheduler schedule-show --name app-1
linode-instance-scheduler schedule-clear --name app-1
```

`schedule-clear` removes that node's own schedule — if it's in a group with a schedule, it follows
the group's schedule again; otherwise it goes back to pure manual `start`/`stop` control. Nothing
about the node itself changes.

**Run the scheduler** — nothing above actually starts/stops anything by itself until this runs:

```
linode-instance-scheduler poll
```

Runs forever, checking every onboarded node's schedule every 15 seconds (`--interval-seconds`
to change it) and starting/stopping whatever is due. A check only reads the local database; the
starts and stops themselves run on separate workers, so a slow start (or a long post-start check)
never delays the checks, and a check also runs the moment any start or stop finishes. A scheduled
action therefore fires within about 15 seconds of its time. Stop it with Ctrl-C: it takes no new
work and waits for any start or stop already running to finish. In production,
run it under a supervisor (e.g. a systemd unit with `Restart=always`) so it comes back up on its
own after a crash or reboot — the same way you'd run any other long-lived process. Prefer cron
over a supervisor? `poll --once` runs exactly one check and exits — schedule that on whatever
interval you'd otherwise poll on.

Every schedule edit takes effect on the next check (within about 15 seconds) automatically —
there's no separate "reload" step, and nothing to restart.

**Large fleets and catching up.** `poll` runs due starts and stops in parallel — up to 10 at a
time by default (`--max-parallel`, up to 50) — so a whole fleet due at 09:00 is started together
rather than one after another. A start or stop stays due for an hour after its scheduled time
(`--window-seconds`, default 3600) for as long as nothing has happened to that node since then.
That means:

- if `poll` was briefly down or a check ran long, the action still happens when it's back;
- if a start fails (for example, the region is temporarily out of capacity for that plan), it's
  tried again after a pause — 1, 2, 5, then every 10 minutes — until it succeeds or the hour is
  up — and each failure
  shows up in `history` and makes `poll --once` exit non-zero, so your monitoring sees it;
- a deliberate manual action after the scheduled time is always respected: stop a node by hand
  at 09:30 and the scheduler won't start it again until its next scheduled start.

A node that's already in the scheduled state (already running at its start time, say) is simply
left alone. When two scheduled times fall within the same hour, the more recent one wins.

How many to run at once is a balance: higher finishes sooner, but every start/stop makes Linode
API calls and needs capacity in the region at the same moment. For very large fleets, spreading
start times a few minutes apart across groups also helps.

**Linode API rate limits.** All parallel workers share one request budget, 10 requests per second
by default (`LINODE_API_MAX_REQUESTS_PER_SECOND`; `0` removes the cap). If Linode does answer
"too many requests" (HTTP 429), every worker pauses for as long as the API asks (its
`Retry-After`), then carries on; other temporary errors are retried with increasing, randomized
delays so workers don't all retry at the same instant. A request that creates something (such as
a new instance) is only ever re-sent after a 429 — never after a gateway error or a dropped
connection, where it might already have gone through — so a retry can't create a duplicate. If
rate limiting persists long enough that a start or stop still fails, it's recorded like any other
failure and tried again on the next check within the catch-up window. Seeing many 429s in the
`poll` output? Lower `--max-parallel` or the request cap.

**Known limitations, for now:**
- If `poll` is down for longer than the catch-up window (default one hour), a scheduled action
  from before the outage isn't fired automatically — acceptable for a cost-scheduling tool (a late
  start/stop costs at most a little extra compute), by deliberate design choice. Such nodes are
  reported as off schedule (an alert, when alerts are set up), and `schedule-catch-up` lists them
  and can apply the missed changes for you (§8.15).

### 8.6 Groups — one schedule for several nodes at once

Have a set of nodes that should all follow the same schedule (e.g. a whole "Dev Environment")?
Create a group instead of setting the same schedule on each node individually — one edit updates
all of them.

**Create a group and give it a schedule:**

```
linode-instance-scheduler group-create --name "Dev Environment" --timezone Asia/Kolkata
linode-instance-scheduler group-schedule-set --group-name "Dev Environment" \
  --timezone Asia/Kolkata --days mon,tue,wed,thu,fri --start-time 09:00 --stop-time 18:00
```

`group-create` makes an empty group (name + an initial timezone); `group-schedule-set` gives it
its actual rules — the same `--days`/`--start-time`/`--stop-time` or `--rules-json` shape
`schedule-set` uses (§8.5), just applied to the whole group at once. **`--timezone` is required
on every `group-schedule-set` call, not just at creation** — it's also how you change a group's
timezone later: just re-run `group-schedule-set` with the new one. Leaving it off isn't an
option that "keeps the existing timezone" — there's no such shorthand, `--timezone` always sets
what the group's rules are resolved against from that point on.

**Add nodes to it** (also works to move a node from one group to another):

```
linode-instance-scheduler group-add --name app-1 --group-name "Dev Environment"
linode-instance-scheduler group-add --name app-2 --group-name "Dev Environment"
```

**A node's own individual schedule (§8.5) always wins over its group's**, if it has one — the
group only governs a node that has no individual schedule of its own. This lets you put a node
in a group for the common case, then give it its own schedule later if it ever needs different
hours, without having to remove it from the group first.

**View or manage the group:**

```
linode-instance-scheduler group-show --group-name "Dev Environment"    # schedule + members
linode-instance-scheduler group-list                                    # every group you have
```

**Remove a node from its group:**

```
linode-instance-scheduler group-remove --name app-1
```

If that node has no individual schedule of its own, this asks what to do — copy the group's
current rules into a new individual schedule for it (so its hours don't change), or leave it
manual-only. Skip the prompt with `--copy-schedule` or `--keep-manual`. If it already has its own
individual schedule, nothing to ask — it was already governed by that, not the group, and removal
just makes that explicit.

**Delete a group entirely:**

```
linode-instance-scheduler group-delete --group-name "Dev Environment"
```

Refuses if the group still has members — `group-remove` each one first. This is deliberate: it
guarantees a group deletion can never silently leave a node with no schedule as a side effect.

Run `poll` the same way as for individual schedules (§8.5) — one poller enforces both individual
and group schedules together, and a group also survives a total local database loss via
`rebuild` (§8), the same tag-based disaster-recovery guarantee everything else in this tool has.

#### Start order between groups (dependencies)

Some nodes only work if another set of nodes is already up — application servers that need their
database, for example. If both groups start at 9am, the app servers can finish booting first,
fail to connect, and stay broken. Tell the tool about the dependency and it orders them for you:

```
linode-instance-scheduler group-depends --group-name app --on db
```

With that in place:

- **Starting:** when `app`'s members are due to start, the scheduler holds them ("waiting on
  dependency") until every member of `db` is running and, if `db` has a post-start hook (§8.12),
  that check has succeeded since its latest start. `app` starts within seconds of `db` being up
  and ready — straight away when the scheduler itself started `db` (it checks again the moment a
  start finishes), otherwise on the next check, at most about 15 seconds later — whatever brought
  `db` up: its own schedule, a different schedule from `app`'s, or a manual start. A chain such as
  `web` → `app` → `db` therefore runs one link after another with no idle time between them.
  Give the database group a readiness check such as `pg_isready -q` so
  "up" means the database is really accepting connections, not just that the machine booted.
- **Stopping:** the reverse — `db`'s members wait until every member of `app` is stopped, and
  stop within seconds once they are.
- The scheduler keeps rechecking every tick for as long as `poll`'s catch-up window allows
  (an hour by default), so a slow database doesn't make the app servers miss their start. If the
  database never becomes ready within that window, the app servers aren't started that day.
- The rule follows **group membership**, so it applies even to a member of `app` that has its own
  individual schedule.
- A group can depend on **several** groups at once — `app` can wait for both `db` and `cache`:

  ```
  linode-instance-scheduler group-depends --group-name app --on db,cache
  ```

  `app` then starts only once every member of **both** is up and ready, and neither `db` nor
  `cache` stops until `app` is down. `--on` replaces the whole list; use `--add <group>` or
  `--remove <group>` to change one entry, and `--clear` to remove them all.
- Several groups can depend on the same group (both `app` and `reports` on `db`); `db` then stops
  only after both are down. Chains of any length are allowed (`web` → `app` → `db`), including
  diamonds (`web` on `app` and `api`, both on `db`).
- A group can't depend on itself, any change that would create a loop through any path is
  refused, and a group that others depend on can't be deleted until they stop depending on it.
- **Manual `start`/`stop` is never blocked** — you're acting deliberately — but it prints a
  warning when the dependency isn't satisfied (e.g. starting an app server while the database is
  stopped).

`group-show` lists what a group depends on and which groups depend on it; `group-list` shows each
group's dependencies. Remove them with:

```
linode-instance-scheduler group-depends --group-name app --clear
```

In the dashboard, tick the groups in the group page's **Start order** card; over the API,
`PATCH /groups/{name}` with `{"depends_on": ["db", "cache"]}` (`[]` clears them).

**Recovery after losing the local database.** Every member of a group carries the group's full
list of dependencies in its disaster-recovery tags, so `rebuild` restores them once the groups
are back. A group with **no members** has no volume to carry tags, so on its own it couldn't be
recovered — with Object Storage configured (§8.10), every group's definition (schedule, hooks
and dependencies) is also kept there, and `rebuild` recreates any group the tags didn't bring
back, then restores the full start order. Without Object Storage, keep at least one member in
any group others depend on, or take regular `backup` snapshots.

### 8.7 Manual override — starting a node outside its own scheduled hours

If a node has a schedule (individual, §8.5, or inherited from a group, §8.6) and you `start` it
manually outside its own scheduled "on" hours, this tool automatically arms a countdown so it
doesn't stay running forever by accident:

```
linode-instance-scheduler start --name app-1
```

```
'app-1' is up at 203.0.113.10.
  manually started outside its scheduled hours -- auto-stops at 2026-08-25 20:00 UTC unless
  extended (`extend --name app-1`).
```

The default window is 2 hours from when you started it. `status` and `list` both show the same
countdown for as long as it's active:

```
linode-instance-scheduler status --name app-1
linode-instance-scheduler list
```

**Need more time?** Extend it explicitly — nothing extends itself automatically, you always have
to ask:

```
linode-instance-scheduler extend --name app-1
linode-instance-scheduler extend --name app-1 --hours 4    # a different window, just this once
```

Each `extend` sets the stop to `--hours` (or the 2h default) from **now** -- but never earlier
than it already was: extending by 1 hour when 3 hours are left keeps the 3 hours. To stop sooner,
just stop it. `poll` (§8.5) is what actually enforces the auto-stop — as long as it's
running (continuously, or on a cron via `--once`), an expired override gets stopped
automatically on the next tick, the same way a normal schedule does.

**Keeping a node running past today's scheduled stop.** For a node running on its schedule (its
own or its group's), `extend` skips today's scheduled stop and keeps it running for `--hours` past
it (or from now, if the stop time has already passed):

```
linode-instance-scheduler extend --name app-1 --hours 3
```

The scheduler holds the scheduled stop until then, and stops the node when the extension runs out.
Tomorrow's schedule is unaffected. In the dashboard: **Keep running past today's stop** on the
node's page. A manual-only node, or one with no active schedule, has nothing to extend.

**A whole group at once:**

```
linode-instance-scheduler extend --group-name app --hours 2
```

Every running member that follows the group's schedule keeps running 2 hours past the group's next
scheduled stop (the group page's **Keep running past today's stop** card does the same). Members
with their own schedule, stopped members and manual-only members are skipped, and listed.

**A node's own extension overrides its group's**, the same way its own schedule does, whichever was
set first. With a 10 PM stop: the group extended by 2 hours (midnight) and one member by 1 hour --
that member stops at 11 PM and the rest at midnight. A group extension skips members with their own
extension ("has its own extension"). Repeating an extension at the same level never makes it stop
earlier.

**Start order is kept.** Extending a node or a group also holds the running members of every group
it depends on (directly or through others) until the same time, when their own stop would come
earlier -- otherwise their stop would be due while the dependent group is still up, wait for it, and
could be missed. When the extension runs out, the dependent group stops first and the groups it
depends on right after it.

**What does NOT get a timer:**
- A `start` that happens to land *inside* your node's own scheduled hours — nothing to revert
  from, the schedule already agrees it should be running.
- A `start` triggered by the scheduler itself, not a person running `start`/`extend` by hand.
- **A node with no schedule at all.** If you're running a node purely manually, on purpose, this
  tool will never impose an auto-stop on it — the override system only ever applies to a node
  that has a schedule to begin with.

**Manually stopping during scheduled "on" hours needs no special handling** — the schedule just
resumes normally at its next window, whether you stopped it manually or not.

`--override-window-hours` on `start` lets you pick a different window than the 2h default for
that one start, if you already know you'll need more (or less) time:

```
linode-instance-scheduler start --name app-1 --override-window-hours 6
```

### 8.8 The REST API (optional) — for a dashboard, or your own scripts

Everything above works purely through the CLI. If you want to build a web dashboard on top, or
call this tool from your own automation over HTTP instead of shelling out to the CLI, run the
API server instead:

```
linode-instance-scheduler serve-api --host 127.0.0.1 --port 8000
```

It binds to `127.0.0.1` by default — reachable only from the same machine, unless you pass a
different `--host` or put it behind your own reverse proxy. It uses the exact same
`LINODE_API_TOKEN` from your `.env` to actually talk to Linode; nothing extra to configure there.

**Logging in.** Instead of a separate password or shared secret, the API uses "Login with
Linode" — your team logs in with the exact same credentials they already use for Cloud Manager.
One-time setup, per deployment:

1. In Cloud Manager: **Profile → OAuth Apps → Create an App**. Set its callback URL to
   `https://<your-host>/oauth/callback` (must match exactly).
2. Copy the **Client ID** and **Client Secret** it gives you into your `.env`, alongside
   `LINODE_API_TOKEN`:
   ```
   LINODE_OAUTH_CLIENT_ID=...
   LINODE_OAUTH_CLIENT_SECRET=...
   LINODE_OAUTH_REDIRECT_URI=https://<your-host>/oauth/callback
   ```

Each deployment registers its **own** OAuth App in its **own** Linode account — this tool doesn't
operate a shared login service anyone else's deployment uses, matching how everything else here
is self-hosted and independent per customer.

**For scripts, use an API token instead** (§8.13) — a login session is meant for people in a browser.

**Using it**: visiting `GET /login` in a browser starts the flow; after logging in with Linode,
`GET /oauth/callback` redirects to `/ui/?session_token=...&expires_at=...` — the web dashboard
(§8.9) picks the token up from those query parameters automatically. Calling `/login`/
`/oauth/callback` yourself from a script instead of a browser works the same way: follow the
redirect chain and read `session_token`/`expires_at` off the final URL's query string, since
there's no separate JSON response carrying them. Every other endpoint requires that token:
`Authorization: Bearer <token>`. `POST /logout` ends the session early.

**What's exposed** — the same operations the CLI has, over HTTP: instance start/stop/status/
history/schedule (`/instances/{name}/...`), the manual-override countdown (`/instances/{name}/
extend`), groups (`/groups`, `/groups/{name}/...`), and two savings numbers —
`scheduled_savings_percent` (computed instantly from a schedule's own rules) and
`actual_savings_percent` (computed from real uptime history over the trailing 7 days by default,
`?days=N` to change that) — at `GET /instances/{name}/savings` and `GET /groups/{name}/savings`.
Full interactive documentation (every endpoint, request/response shapes) is auto-generated at
`/docs` once the server is running.

**A credential a Linode login can't replace**: the poller (`poll`, §8.5) has to keep running with
no one logged in, so it — and every API call the server itself makes to Linode — still goes
through your deployment's own `LINODE_API_TOKEN`, exactly as before. Logging in with Linode only
ever proves *who* is calling this API; it's never used to talk to Linode directly.

**Who can sign in.** Only users of the Linode account this deployment manages can sign in: after Linode confirms who someone is, the tool checks that username against the account's users (read with `LINODE_API_TOKEN`, which needs Account (Read Only) for this) and refuses anyone else. Set `LIS_DASHBOARD_USERS` (comma-separated Linode usernames) to allow only those users; if the token has no Account access, this list is required, otherwise nobody can sign in. The check is repeated every 15 minutes for signed-in users, so someone removed from the account or the list loses access. A refused user sees the reason on the sign-in page.

**What each person may do (teams and roles).** Signing in proves who someone is; what they may then do comes from their role. The Linode account's unrestricted users are admins automatically, and `LIS_ADMIN_USERS` (comma-separated Linode usernames) names more. Admins have full access and manage everyone else on the dashboard's **Users & teams** page (or with `users`, `team-create`, `team-update`, `team-add-member`, `team-remove-member`, `team-rule-add`, `team-delete`). A team has a role and, optionally, a list of schedule groups and/or nodes it covers (all of them if left out):

| Role | May |
|---|---|
| `viewer` | see instances, groups, schedules, savings, history and activity |
| `operator` | viewer, plus start, stop, extend, start/stop a group and run a hook |
| `editor` | operator, plus change schedules, groups, membership, start order, holidays and settings |
| `admin` | everything, including onboarding, offboarding, hooks, API tokens, logs, the console and people |
| `custom` | exactly the operations you choose -- the same operation names as API tokens (e.g. `instances:start` alone, or `read` plus `hooks:run`) |

A custom team is created with `team-create --name starters --role custom --scopes instances:list,instances:status,instances:start --groups web`, or by choosing "custom" under Permissions on **Users & teams** and picking the operations (`api-token-scopes` lists them). **Edit** on a team changes its permissions and the schedule groups and nodes it covers (`team-update`); giving `--scopes` makes a team custom, and choosing a preset role again drops its own list. The **People** list shows every user of the Linode account, including those who haven't signed in yet, so access can be given before someone's first sign-in.

A person can belong to several teams; each team counts on its own, so "viewer of everything" plus "operator of the `web` group" lets them see all nodes but start and stop only the `web` group's. A username can be added to a team before the person ever signs in. A sign-in rule puts people whose Linode email has a given domain into a team automatically (`team-rule-add --team payments --email-domain payments.example.com`). Anyone else who signs in sees a "waiting for access" page, and the Activity log tells admins someone is waiting. The menu leaves out pages a person's role can't use, and the server refuses them regardless. Teams, members and rules are kept in Object Storage when it is configured and come back with `restore` or `rebuild`. If the deployment's token can't read the account's users and no `LIS_ADMIN_USERS` is set, there is no way to tell admins apart, so everyone allowed to sign in keeps full access until one of those is set.

### 8.9 The web dashboard (optional) — a browser UI on top of the REST API

If you'd rather click around than script against the REST API directly: the dashboard is part of
every release and is served by the same `serve-api` process. Visit `https://<your-host>/` (or
`/ui/`) in a browser. Nothing else to configure — it talks to the same API server it's served
from, using the same OAuth setup as §8.8.

**What it covers**: log in with the same "Login with Linode" flow §8.8 describes (a "Log in with
Linode" button, instead of visiting `/login` by hand); an Onboard page that picks an existing
Linode instance from your account and brings it under management (including a one-time SSH
password/key field for a node that doesn't yet trust this deployment's own key — it's used only
for that one attempt and never stored, see §3 above) — and, if that instance still
needs Path B migration (§6) first, a guided wizard for it. At the copy step the wizard shows the
same single command the CLI prints (§6.2): it finds both disks itself, copies only the blocks in
use, and prints `COPY_DONE`; you paste the last lines of its output back, and "finish the
migration" stays disabled until they show `COPY_DONE` (and no `COPY_FAILED`). The start screen
also offers "Two-phase copy — short downtime" (§6.6.1), and with a backup kept, "Run the copy for
me" (§6.6). The older step-by-step path is still there behind "Use the step-by-step whole-disk dd
instead", deliberately never printing a `dd` command up front: (1) you run `lsblk` in
Lish and paste the output back — the page identifies which device is your original disk and which
is the new, empty volume purely by matching sizes against the real source-disk and
destination-volume sizes it already knows from the Linode API (entirely in your own browser — no
external service, nothing sent anywhere for this check), and shows you its best guess to confirm
or correct via two dropdowns before anything is built; (2) only once you've confirmed which device
is which does it build the actual `dd` command, with a working Copy button; (3) after you run it,
you paste `dd`'s own summary output back, and the page checks it for a clean completion (matching
"records in"/"records out" counts, no I/O error text) — output that shows the copy failed blocks
the next step outright, with the reason shown; output with no recognisable summary gets a warning
but doesn't block it. This
is an early, best-effort check on top of the real safety net, not a replacement for it — the
actual proof that the migration worked is still the root-device identity check performed over SSH
once you click "finish migration" (the CLI takes a different route to the same safety: its
printed copy command identifies the two disks itself and refuses if it can't, §6.2). A list of every onboarded
instance with its current status; a per-instance detail page for start/stop/extend, viewing and
editing its individual schedule, its scheduled-vs-actual savings percentages, its recent event
history, and two distinct one-way actions in its own separate cards — **Offboard** (permanently
decommissions the node on Linode too, after the grace period — releases the reserved IP,
optionally deletes volumes, see the `offboard` CLI section above) and **Remove from tracking** (the `deregister` CLI command's
dashboard equivalent — removes local tracking only, never touches the node itself, for a record
that's simply wrong rather than a node you're actually done with); a **Pending deletion** page
listing offboarded nodes and deleted backups still inside their grace period, each with **Recover**
and **Delete now** (after typing the name); a group list and per-group detail page for the same
schedule/savings view plus membership management. It's a thin client
over the REST API in §8.8 — every action it takes is one of that API's own endpoints, nothing the
dashboard can do that the CLI/API couldn't already do directly.

### 8.9.1 Activity, Logs and Console — seeing and running things from the dashboard

Three dashboard pages show what the backend is doing, without opening a terminal on the
scheduler host:

- **Activity** — everything the backend did and why, newest first, updating live: each start,
  stop, onboard, offboard, migration step and hook run (scheduled, manual or from the dashboard),
  every warning and error they reported, each scheduler decision (fired, waiting on a dependency,
  failed), and every console command with who ran it. Filter by level, source or text. A banner at
  the top says when the scheduler last checked in, and turns red if it stops — so a schedule that
  didn't fire because the scheduler wasn't running is obvious at a glance. Each instance's page
  has the same feed for just that instance ("Activity log"). Entries are kept for 30 days
  (`ACTIVITY_LOG_RETENTION_DAYS` in `.env`).
- **Logs** — the raw output of the scheduler (`poll`), the API/dashboard server and backups,
  tailed live, plus the console's own log and the database file's size. Each assisted migration
  copy's Rescue Mode console session is listed there too (**Migration console sessions**), one per
  instance, readable as plain text and updating live while a copy runs. A session can be deleted
  there (**Delete session**) and is removed automatically after the retention period. Engine messages appear in
  whichever service ran the operation. The same files are on disk under `state/logs/` (rotated at
  5 MB, three old files kept), so they work identically on a VM and on Kubernetes.
- **Console** — runs this tool's own commands on the scheduler host with live output: `list`,
  `status --name web-1`, `history`, `start`, `stop`, `reset-host-key`, `rebuild`, `backup` and
  the rest. It is not a shell: only this tool's commands run, so the console can't read the Linode
  API token or the SSH key, or run other programs. A command that normally asks for confirmation
  needs `--yes`. The long-running services (`poll`, `serve-api`), `restore`, SSH key backup and
  creating API tokens aren't available there, and neither are the long migration commands:
  `migrate-wave` (use `POST /migrations/waves`), `migrate-copy` (use the Migrate page or
  `POST /instances/<name>/migrate-copy`) and `migrate-start --two-phase` (use the CLI on the host). A console command is stopped after 3 hours. The console is for dashboard logins only (never API
  tokens), and every command is recorded on the Activity page and in the console log.

### 8.10 `backup` — scheduling your own whole-system backups

**Where backups go, and whether they work: `backup-config`.** `install.sh` asks for Object Storage
settings and sets up a scheduled `backup` (hourly by default). Without Object Storage, backups stay
on the same host -- lost with it -- so set it up if you skipped it, and check on it from time to
time:

```
linode-instance-scheduler backup-config                       # status and the last backup's result
linode-instance-scheduler backup-config --bucket my-bucket \
    --endpoint https://in-maa-1.linodeobjects.com               # prompts for the access/secret key
linode-instance-scheduler backup-config --test                 # write/read/delete a test object
linode-instance-scheduler backup-config --disable              # remove the Object Storage settings
```

New settings are tested (a small test object is written, read back and deleted) and saved to
`.env` only if the test passes; the secret key is never shown again. A running scheduler and API
pick up the change within seconds, no restart needed. The dashboard's **System backup** page does
the same, plus **Back up now** and the last backup's result.

Every stop/onboard already backs that one node up automatically (see the disaster recovery
section of the Definitive Guide) — `backup` is a separate, explicit command for taking a backup
of *everything at once*, on your own schedule, rather than waiting for individual nodes to be
touched:

```
linode-instance-scheduler backup --backup-dir /var/backups/instance-scheduler
```

This does two things every time it runs: if Object Storage is configured (see the Operations
Guide), it re-syncs every currently-onboarded node's own Object Storage record in one pass, and
uploads a single, consistent snapshot of your entire local database there too — covering
schedules and groups, which the per-node records don't. If `--backup-dir` is given, it also
writes that same snapshot to a local file. Give it one, the other, or both; it refuses with a
clear error if neither is available, since there'd be nothing to actually do.

Run it on whatever schedule matches your own risk tolerance — a daily cron entry is a reasonable
default:

```
# /etc/cron.d/instance-scheduler-backup
0 3 * * * root linode-instance-scheduler backup --backup-dir /opt/linode-instance-scheduler/backups >> /var/log/instance-scheduler-backup.log 2>&1
```

Exits non-zero if anything didn't complete (a per-node re-sync failure, either snapshot
destination failing) — check the exit code if you're wiring this into your own monitoring rather
than just reading the log. Each run also saves the trusted SSH host keys of your nodes (the tool's
own `state/known_hosts`), to Object Storage and next to the local snapshot.

**If you install with `install.sh`** (see `DEPLOYMENT.md`), an hourly backup timer is set up for you.

**Backup on every change.** With Object Storage configured, the scheduler also uploads a fresh
database snapshot within about a minute of any change (`full-backups/instances-continuous.db`,
overwritten each time), so a lost host loses at most about a minute of changes. Old timestamped
snapshots are pruned after each hourly backup: everything from the last 48 hours, then one a day
for 30 days (`LIS_BACKUP_KEEP_ALL_HOURS`, `LIS_BACKUP_KEEP_DAILY_DAYS`). The newest snapshot and the
continuous one are never deleted.

**Snapshots are encrypted.** Every database snapshot written to Object Storage is encrypted
(AES-256-GCM) with a key derived from `LIS_SSH_KEY_PASSPHRASE`, the deployment passphrase below, so
reading the bucket alone doesn't reveal it. Restoring needs that passphrase: a replacement host gets
it from your Linode account (`install.sh recover` does this for you, or `ssh-key-restore
--save-passphrase`). Snapshots written by older versions are not encrypted and still restore. Without
a passphrase set (some Kubernetes setups), snapshots are uploaded unencrypted; set it to encrypt them.

**The deployment SSH key.** Every managed node trusts exactly one key, the deployment's own, so it's
kept recoverable automatically: encrypted in the database (and therefore every snapshot) and in
Object Storage, with `LIS_SSH_KEY_PASSPHRASE` from `.env` (generated at install, shown once); its
public half under your Linode profile's SSH keys; and the passphrase in a private StackScript in
your Linode account (`lis-recovery-…` — don't run or delete it). A replacement host fetches the
passphrase with the API token it needs anyway, so nothing extra has to be remembered. Anyone with a
full-access Linode token could already get root on every instance (rescue mode, root password
reset, Lish), and the passphrase opens nothing without the encrypted key, which needs your Object
Storage keys or a snapshot. Check it any time:

```
linode-instance-scheduler ssh-key-protect
```

**Your own encrypted copy of the settings.** First create an age key on your own computer (not the
server): install age (`brew install age` on macOS, `sudo apt install age` on Ubuntu/Debian,
`sudo dnf install age` on Fedora/RHEL, `winget install --id FiloSottile.age` on Windows), then run
`age-keygen -o lis-age-key.txt`. It prints `Public key: age1…`; keep `lis-age-key.txt` (the secret
part) in your password manager. Set that public key and the scheduler keeps the whole `.env` and the deployment SSH
key in your bucket, encrypted so only your `key.txt` opens them — this host can write them but not
read them back. They're re-uploaded whenever either changes:

```
linode-instance-scheduler env-backup --set-recipients age1...      # once; also on the System backup page
linode-instance-scheduler env-restore --identity-file key.txt --ssh-key keys/deploy_key   # on a new host
linode-instance-scheduler env-restore --identity-file key.txt --print   # just show them (e.g. for a Kubernetes Secret)
```

On a new host, `sudo ./install.sh recover --env-from-backup --age-identity key.txt` asks only for
the bucket settings.

**Moving to a new host.** If the machine running this tool is lost, `sudo ./install.sh recover` on a
fresh one restores everything before starting anything — see `DEPLOYMENT.md` §8.1. The pieces it
uses are also available directly:

```
linode-instance-scheduler ssh-key-restore --save-passphrase   # the deployment SSH key (passphrase from your Linode account)
linode-instance-scheduler restore --list     # the snapshots in your bucket
linode-instance-scheduler restore            # newest snapshot + trusted host keys (or --snapshot / --from-file)
linode-instance-scheduler rebuild            # adds anything newer than the snapshot, from tags and Object Storage
linode-instance-scheduler heal               # adopts instances the snapshot doesn't know about (§8.15)
linode-instance-scheduler schedule-catch-up  # nodes left off their schedule by the outage (§8.15)
```

`restore` refuses to overwrite a database that already has instances or groups unless you pass
`--force`, which moves it aside rather than deleting it. A snapshot from an older version is
upgraded automatically. `rebuild` also finds a node whose tags were removed, through its Object
Storage record.

**Putting back one thing, not the whole database (`restore-item`).** If one node's schedule or
hooks, or one group, was changed or deleted by mistake, bring just that item back from a snapshot:

```
linode-instance-scheduler restore-item schedule --name web-1 --show        # what the newest snapshot holds
linode-instance-scheduler restore-item schedule --name web-1               # put it back
linode-instance-scheduler restore-item hooks --name db-1 --snapshot full-backups/<key>.db
linode-instance-scheduler restore-item group --name dev --with-members     # also re-add its members
linode-instance-scheduler restore-item group --name dev --from-file /var/backups/instance-scheduler/<file>.db
```

The newest snapshot in Object Storage is used unless you name one (`restore --list` lists them) or
give a local file. A schedule or hooks need the node onboarded now. A group is created if it no
longer exists, with its schedule, holiday setting, hooks and start order from the snapshot (a
start-order target that no longer exists is skipped and reported); `--with-members` also adds back
members that exist and aren't in another group. On the dashboard: **System backup → Restore one item
from a snapshot**, which shows the snapshot's version next to what's there now before restoring.
Over the API: `GET /system/snapshots`, `POST /system/restore-item/preview` and
`POST /system/restore-item` with `{"kind", "name", "snapshot", "with_members"}`.

**A copy of the release in your bucket (`release-backup`).** Installing on a new host normally
downloads this tool from GitHub. So that a recovery doesn't depend on GitHub being reachable, the
scheduler keeps a copy of the installed release (the program, its built dashboard and its
installer) in your bucket, at `deployment/release/<version>.tar.gz` and
`deployment/release/latest.tar.gz`. It's uploaded whenever the installed release changes, and
again if it's been removed from the bucket. To upload now:

```
linode-instance-scheduler release-backup                  # if anything changed
linode-instance-scheduler release-backup --force          # even if unchanged
linode-instance-scheduler release-backup --output /tmp/release.tar.gz   # a local file instead
```

Over the API: `GET /system/release-copy` (status) and `POST /system/release-copy` (`?force=true`).

To use it on a new host, fetch it with any S3 tool and your bucket's access keys, extract it, and
run the installer from inside it — nothing is downloaded:

```
aws s3 cp s3://<bucket>/deployment/release/latest.tar.gz . --endpoint-url https://<region>.linodeobjects.com
tar xzf latest.tar.gz
cd linode-instance-scheduler && sudo ./install.sh recover
```

**Bucket versioning.** When backups are configured, versioning is turned on for the bucket, with a
lifecycle rule that removes old versions 30 days after they were overwritten or deleted (any other
lifecycle rules on the bucket are kept). An overwritten or deleted backup can then be brought back
within those 30 days. The health check turns it on again if it's found off. The bucket's access key
needs permission to change bucket settings; if it doesn't, an alert says so.

```
linode-instance-scheduler bucket-protection                       # status
linode-instance-scheduler bucket-protection --enable              # turn it on now
linode-instance-scheduler bucket-protection --key full-backups/instances-continuous.db     # its versions
linode-instance-scheduler bucket-protection --key full-backups/instances-continuous.db \
    --restore-version <VERSION_ID>                                 # make that version current again
```

Bringing a version back copies it as the newest version, so every version is still kept. Over the
API: `GET /system/bucket-protection` and `POST /system/bucket-protection` (turn it on).

**Every release is checked before it's installed.** Each release archive has a published
SHA-256 checksum; `install.sh` refuses a downloaded archive that doesn't match it, and checks an
archive you give it with `--archive` when its `.sha256` file is next to it (keep the two together,
or run `sha256sum -c` yourself first).

### 8.11 High availability — why this isn't an always-on active-active or active-passive setup

Run exactly **one** `poll` process at a time, on one machine — never two running simultaneously,
and never a hot standby waiting in the wings. That's not a corner cut; it's the right fit for
what `poll` actually does.

`poll` doesn't serve live requests — it checks every schedule on a short, fixed interval, and a due
action stays due for up to an hour (§8.5). If that process is briefly down (a crash, a host
reboot, a deploy), the worst case is a due action firing a little late once your process
supervisor brings it back up — nothing waits on it
in real time, so nothing downstream notices. Manual `start`/`stop` keeps working the entire time
regardless, since it never depends on `poll` being up.

An **active-active** setup (two schedulers running at once) would trade that harmless delay for
a real correctness risk instead: two processes acting at the same time could race to act on the
same node at the same moment, or have one fire a start while the other is
mid-firing a stop for it — a new failure mode, introduced to solve a problem (a few seconds of
downtime) a plain restart already solves for free. An **active-passive** setup avoids the double-
fire risk, but only by adding its own always-on standby plus real failure-detection machinery
(health checks, a way for the standby to agree it should take over, a shared view of state) —
infrastructure that has to be running and paid for continuously, to protect against an outage a
single supervised process already recovers from in seconds. (With Object Storage backups
configured, the scheduler holds a lease there, so a second scheduler started by mistake stands by
and does nothing until it's told to take over — see §8.15.)

That's also, directly, why it saves you money rather than costs you any: this whole tool exists
to stop you paying for compute that isn't earning its keep. Running a second, always-on
scheduler instance as insurance against a multi-second restart would mean paying for exactly
that kind of idle, always-on capacity — just moved onto this tool's own infrastructure instead
of your managed fleet. The approach actually used costs nothing extra: one process, under
whatever process supervisor your OS already has built in, on the one machine you're already
running this from. If the underlying data is ever lost too, `rebuild` and the Object Storage
backup (§8.10, above) recover it without needing a second live system standing by either — see
the Definitive Guide's Deployment Model chapter for the fuller comparison.

---

### 8.12 Hooks — run your own commands before a stop and after a start

Some services want a step of their own around a stop/start cycle. A database might need a clean,
checked shutdown before its instance is deleted, and you may want to confirm it's actually
accepting connections again after the instance comes back. Hooks let you attach your own commands
to both moments:

- **Pre-stop hook** — runs once on the node, right before it's shut down and deleted.
- **Post-start check** — runs on the node after it's reachable again, as a readiness check.

Both run as `root` over the same SSH connection this tool already uses, and apply to every stop
and start: scheduled, manual, through the API or dashboard, and the automatic stop at the end of
a manual-override window.

**Every stop already shuts the operating system down gracefully** before deleting the instance,
so services managed by systemd (PostgreSQL, Redis, and so on on a normal install) are stopped
cleanly even without a hook. A pre-stop hook adds an explicit step whose result is checked, and
the option to keep the node running if it fails.

```
linode-instance-scheduler hooks-set --name pg-1 \
    --pre-stop "pg_ctlcluster 16 main stop -m fast" \
    --post-start "pg_isready -q"
```

A hook can be given two ways:

- **A command** — run as-is on the node. That's either an inline command (`pg_isready -q`) or the
  path of a script you already ship on the node, for example with your own image or configuration
  management (`/opt/app/hooks/before-stop.sh`). The script itself stays on the node's OS volume,
  which survives every stop/start.
- **An uploaded script** — the script text itself, stored by this tool. Each time the hook runs,
  it's copied to a temporary file on the node, run (its own `#!` line picks the interpreter), and
  deleted afterward. Up to 1 MB.

```
linode-instance-scheduler hooks-set --name pg-1 --pre-stop-script ./before-stop.sh
```

Options (only the ones you pass change; anything already set is kept):

| Option | Meaning |
|---|---|
| `--pre-stop COMMAND` | The pre-stop hook, as a command or a path on the node. |
| `--pre-stop-script FILE` | The pre-stop hook, as an uploaded script (read from a local file). |
| `--pre-stop-timeout SECONDS` | How long it may run (default 300, max 3600). |
| `--pre-stop-on-failure abort\|continue` | What happens if it fails — see below (default `abort`). |
| `--post-start COMMAND` | The post-start check, as a command or a path on the node. |
| `--post-start-script FILE` | The post-start check, as an uploaded script. |
| `--post-start-timeout SECONDS` | Total time allowed for the check to pass (default 600, max 3600). |
| `--clear-pre-stop` / `--clear-post-start` | Remove just that one hook. |

Hooks are configured per node or per group. `--group-name dbs` instead of `--name pg-1` sets them
for every member of a group. A node's own hook overrides its group's **for that hook type only**,
so a node can set its own post-start check and still use the group's pre-stop hook.
`hooks-show --name pg-1` shows both its own hooks and the ones that actually apply, including
which group each inherited one comes from.

A command can be up to 4096 characters; for anything longer, upload it as a script or ship it on
the node and give its path. A timed-out hook's command is not killed on the node — if you need a
hard limit there, wrap your command in `timeout`.

**What happens if a hook fails.** A hook fails when its command exits with a non-zero code, runs
past its timeout, or the node can't be reached over SSH to run it at all.

- **Pre-stop hook fails, policy `abort` (the default):** nothing is shut down or deleted. The node
  keeps running (and billing), the stop is recorded as failed in `history`, the command exits
  non-zero, and the end of the hook's own output is printed so you can see why. It's not retried
  on its own: fix the cause and run `stop` again, or use `stop --skip-hooks` to stop without the
  hook. If your hook does several things, note that it may have done some of them before
  failing (for example, stopped the database but then exited non-zero) — this tool doesn't undo
  that, so make your script restore what it changed if a partial run matters.
- **Pre-stop hook fails, policy `continue`:** a warning is recorded and the stop goes ahead.
- **Post-start check:** it's retried every 15 seconds until it succeeds or its timeout runs out,
  so "not ready yet, 20 seconds after boot" is normal and simply retried. If it never succeeds,
  the node is **left running** — it's never stopped or deleted automatically for this — and
  the start is reported as failed so someone can look at it. `status` shows when the last check
  failed. Once you've fixed the cause, re-run the check without restarting:

```
linode-instance-scheduler hooks-run --name pg-1 --post-start
```

`start --skip-hooks` starts a node without running its post-start check, and
`stop --skip-precapture` (for a node that can't be reached over SSH) skips the pre-stop hook too,
since there's no way to run it, as does a stop of an instance that is already powered off. Both are recorded in the node's hook history.

`hooks-run --name pg-1 --pre-stop` runs the pre-stop hook on demand. It asks for confirmation
first (`--yes` skips the prompt), because the node is **not** stopped afterward, so whatever the
hook stops stays stopped until you restart it.

**History.** Every hook run (result, exit code, the end of its output) and every hook change (who
changed it, when, and what it was changed to) is recorded. The dashboard's Hooks card on each node
shows this, and the API exposes it at `GET /instances/{name}/hook-events`.

**Recovering hooks after losing the local database.** Hooks are kept in the local database
alongside schedules and groups (so `backup`, §8.10, covers them), and are also recorded where
`rebuild` can find them with no local database at all:

- **With Object Storage configured (recommended):** every hook — command, script path, or uploaded
  script, for a node or a group — is stored in your bucket, and the node's OS volume gets a short
  tag pointing at it (`hk-…` for its own hooks, `hkg-…` for its group's). `rebuild` reads the tags,
  fetches each hook, checks that its content matches the fingerprint in the tag, and restores it
  on the right node and group. A hook is stored in Object Storage *before* it takes effect: if that
  write fails, the change is refused and nothing changes, so a configured hook is always
  recoverable.
- **Without Object Storage:** a short command (up to about 40 characters, such as a script path)
  is written directly into the node's tags and restored the same way. A longer command or an
  uploaded script can't fit in a tag, so it's kept locally only — `hooks-set` warns when that
  happens, and only a `backup` snapshot can bring it back.

Hooks are never stored on the node by this tool (an uploaded script only exists there while it
runs), and the fingerprint check means a hook can't be swapped for different content in your bucket
or by editing a tag without `rebuild` refusing to restore it.

### 8.13 Manual-only nodes and scripting

Some nodes shouldn't follow any schedule at all — you start and stop them yourself, or from your
own scripts and pipelines. This section covers making a node manual-only, and everything a script
needs: a credential, predictable results, and acting on a whole group in one call.

#### Manual-only nodes

```
linode-instance-scheduler set-mode --name build-runner-1 --manual
```

A manual-only node is never started or stopped by the scheduler, and a manual start never arms an
auto-stop timer. Start and stop it with `start`/`stop`, the dashboard, or the API. It shows as
`[manual-only]` in `list` and has a "Manual-only" card on its dashboard page.

A manual-only node can't have a schedule, and can't be in a group that has a schedule:

- `set-mode --manual` refuses if the node has its own schedule (`schedule-clear` it first) or is in
  a group with a schedule (`group-remove --keep-manual` it first).
- `schedule-set`, `group-add` into a scheduled group, and `group-schedule-set` on a group with a
  manual-only member are refused the same way.
- It *can* be in a group without a schedule — useful for sharing hooks (§8.12) or a start order
  (§8.6) with other nodes.

Switch it back with `set-mode --name build-runner-1 --auto`. The setting is saved in the node's
disaster-recovery tags, so `rebuild` restores it.

#### Starting or stopping a whole group

```
linode-instance-scheduler start --group-name dev
linode-instance-scheduler stop  --group-name dev --yes
```

Every member is acted on at once (up to `--max-parallel`, default 10), and each succeeds or fails
on its own — the output lists every member and its outcome. If the group has a start order
(§8.6), add `--with-dependencies` to bring up the whole chain in order — `start` starts the groups
it depends on first and waits for them to be ready (post-start checks included) before starting
this one; `stop` stops the groups that depend on it first. A stage that doesn't fully succeed stops
the chain, and the later groups are reported as `skipped`.

#### Start order for one-off actions

By default a manual `start`/`stop` goes ahead even when the node's start order isn't satisfied,
with a warning. Add `--respect-dependencies` to refuse instead — the right choice for unattended
scripts:

```
linode-instance-scheduler start --name web-1 --respect-dependencies
```

#### Exit codes and JSON output

`start` and `stop` (single node or group) exit with:

| Code | Meaning |
|---|---|
| 0 | Done, or already in that state |
| 1 | Failed (see the message) |
| 2 | Command-line usage error |
| 3 | Busy — another operation is running on this node; retry later |
| 4 | Security warning — the node's SSH host key changed; don't retry blindly (see `reset-host-key`) |
| 5 | Refused by `--respect-dependencies` — the start order isn't satisfied yet |
| 6 | Refused by a budget or guardrail — ask for approval (`approval-request`), see §8.18 |

Add `--json` to `start`, `stop` or `list` to get one JSON object on stdout (progress messages go
to stderr). `stop --json` needs `--yes`. `status` already prints JSON.

#### API tokens

Scripts calling the REST API (§8.8) use an API token rather than a browser login:

```
linode-instance-scheduler api-token-create --name ci-deploy --scopes read,operate --groups dev \
  --expires-days 90
```

The token is printed once — store it in your secret manager. Send it as
`Authorization: Bearer <token>`. You can also create, list and revoke tokens on the dashboard's
**API tokens** page. Only a hash of each token is kept, so a lost token can't be shown again;
revoke it and create a new one. With Object Storage configured, each token's record (hash, scopes,
limits, expiry, revocation; never the token) is also written there on every create and revoke,
so tokens survive a lost database and a restore from an older snapshot never brings a revoked
token back. If that write fails you'll see a warning; run `backup`, or revoke the token again, to
retry.

Scopes come in two sizes. The four **bundles** cover common roles:

| Bundle | Allows |
|---|---|
| `read` | List, status, history, savings, activity, schedules, groups, hooks, pending deletions, snapshots, and backup/system status (view only) — `instances:list`, `instances:status`, `instances:history`, `savings:read`, `activity:read`, `schedules:read`, `groups:read`, `hooks:read`, `deletions:read`, `backups:read`, `system:read`, `system:doctor` |
| `operate` | Start, stop, extend, run a hook now, group start/stop, clear a stuck mark, catch up missed schedules, run a backup, save the release copy — `instances:start`, `instances:stop`, `instances:extend`, `groups:start`, `groups:stop`, `hooks:run`, `instances:clear-lock`, `schedules:catch-up`, `system:backup`, `backups:release-copy` |
| `configure` | Schedules, groups, group membership, start order, manual-only mode, VPC address, recovering a pending deletion, restoring one item, bucket versioning, recovery drills, system settings — `schedules:write`, `groups:write`, `groups:membership`, `dependencies:write`, `mode:write`, `instances:vpc-address`, `deletions:recover`, `backups:restore-item`, `backups:versioning`, `backups:drill`, `system:configure` |
| `admin` | Everything, including changing hooks (they run as root), onboard/offboard, registering LKE node pools, migration, logs, deleting a pending deletion now, rotating credentials, and managing tokens |

For a token that should do exactly one thing, give it **single operations** instead (or as
well) — for example a nightly job that may only stop nodes in one group:

```
linode-instance-scheduler api-token-create --name nightly-stop --scopes instances:stop \
  --groups dev
```

| Operation | Allows |
|---|---|
| `instances:list` | List nodes |
| `instances:status` | One node's status |
| `instances:history` | One node's history |
| `savings:read` | Savings figures (node or group) |
| `activity:read` | The activity log |
| `logs:read` | Service log files |
| `instances:start` / `instances:stop` | Start / stop a node |
| `instances:extend` | Extend a manual-override timer |
| `instances:clear-lock` | Clear a stuck in-progress mark (refused while a start/stop really runs) |
| `schedules:catch-up` | Start/stop nodes that missed their schedule, after review |
| `groups:start` / `groups:stop` | Start / stop a whole group |
| `schedules:read` / `schedules:write` | Read / set or clear a node's schedule |
| `groups:read` | List and view groups |
| `groups:write` | Create or delete groups, set a group's schedule |
| `groups:membership` | Add a node to, or remove it from, a group |
| `dependencies:write` | Set a group's start order |
| `mode:write` | Make a node manual-only or schedulable |
| `hooks:read` / `hooks:write` / `hooks:run` | Read hooks / set or clear them (they run as root) / run one now |
| `instances:onboard` | List account instances, reserve an IP, onboard |
| `instances:migrate` | Migrate a node off local disk |
| `instances:offboard` | Offboard a node or remove it from tracking |
| `instances:vpc-address` | Move a stopped node to another VPC address |
| `pools:register` | Schedule an LKE node pool, or change how it's stopped |
| `instances:readiness` | Check every instance on the account for onboarding (read-only; in no bundle, so `admin` or this scope) |
| `tokens:manage` | Create, list and revoke tokens — never one wider than itself |
| `system:read` | Backup, key-protection, restore-test, lease, watchdog and alert status |
| `system:doctor` | Run the consistency check (`doctor`); read-only |
| `system:backup` | Run a backup, a restore test, the settings backup, key protection |
| `system:configure` | Heal, take over the scheduler lease, Shutdown Watchdog, the age key for the settings backup |
| `deletions:read` | List nodes and backups pending deletion |
| `deletions:recover` | Recover a node or backup pending deletion |
| `deletions:delete-now` | Carry out a pending deletion now (it can't be recovered afterwards) |
| `backups:read` | List database snapshots and preview an item in one |
| `backups:restore-item` | Restore one schedule, a node's hooks, or a group from a snapshot |
| `backups:release-copy` | Save a copy of the installed release to Object Storage |
| `backups:versioning` | Turn on bucket versioning, list and bring back object versions |
| `backups:drill` | Run a recovery drill (creates and deletes a throwaway VM) |
| `credentials:rotate` | Rotate the Linode API token, bucket keys, OAuth secret or deployment SSH key (admin-level: it replaces credentials) |

Settings that hold credentials -- Object Storage keys, the SMTP password -- can only be changed with
an `admin` token or by a dashboard user with the admin role.

`linode-instance-scheduler api-token-scopes` prints this list. A request without the scope it
needs is refused with `403` naming the missing scope; anything not on this list needs `admin`.

`--instances` and `--groups` limit a token to particular nodes and/or groups (a group covers its
current members). Manual-only nodes can be included too; the token can then start and stop them
like any other. On the dashboard's **API tokens** page, nodes and groups are picked from searchable
lists of what exists, and the operations shown come from the server (`GET /tokens/scopes`), so the
list always matches this version. A limited token only sees those nodes and groups in lists, and can only read
operations it started itself. Every action taken with a token is recorded in `history` under
`token:<name>`. `api-token-list` shows each token's scopes, limits, expiry and when it was last
used; `api-token-revoke --name ci-deploy` stops it working immediately.

#### Waiting for the result over the API

`POST /instances/{name}/start` and `/stop` normally return at once with an operation id to poll
(`GET /operations/{id}`). Add `?wait=true` to get the finished result in the same response —
one call per action from a script:

```
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"respect_dependencies": true}' \
  "https://<your-host>/instances/web-1/start?wait=true"
```

The body is the same as a finished `GET /operations/{id}`: `status` (`done` or `error`), `result`
(the outcome, e.g. `started`), and any `warnings`. An HTTP 409 means it was refused — busy, or the
start order isn't satisfied with `respect_dependencies`. A wait longer than an hour returns 202
with the still-running operation to keep polling. Group actions are
`POST /groups/{name}/start|stop` with `{"with_dependencies": true}` (optional), and also accept
`?wait=true`. Switch a node to manual-only over the API with `PATCH /instances/{name}` and
`{"schedule_mode": "manual"}`.

### 8.14 Holidays — keep everything down on a date it would normally run

Tomorrow is a holiday and nothing should start:

```
linode-instance-scheduler holiday-add --date 2026-10-12 --note "Dussehra"
```

On that date the scheduler skips every node's scheduled starts. Scheduled stops still happen, so
anything left running the evening before is shut down as usual, and the next normal day starts on
schedule -- nothing to remember to undo.

- **A range:** `holiday-add --date 2026-12-24 --to 2026-12-31`.
- **Only some nodes:** `--group-name dev` (that group's members) or `--name web-1` (one node).
- **The date is each schedule's own local date** -- a group on `Asia/Kolkata` uses the Indian date.
- **Manual and API starts are never blocked** -- you can still start a node by hand on a holiday.
- **Changed your mind?** `holiday-remove --date 2026-10-12` (same `--group-name`/`--name` as when
  added). A start skipped earlier that day catches up within the usual hour.
- `holiday-list` shows what's coming up (`--all` includes past dates).

**Groups or nodes that must keep running on account-wide holidays.** A holiday added without
`--group-name`/`--name` is account-wide and applies to every node by default. A group can opt out:

```
linode-instance-scheduler holiday-settings --group-name prod --ignore-account-wide
```

Its members then start as usual on account-wide holidays. Holidays added for that group (or for
one of its nodes) still apply. A node's own setting always overrides its group's, the same way its
own schedule does:

```
linode-instance-scheduler holiday-settings --name web-1 --follow-account-wide   # skips them even if its group runs
linode-instance-scheduler holiday-settings --name db-1 --ignore-account-wide    # runs on them, with or without a group
linode-instance-scheduler holiday-settings --name web-1 --inherit               # back to the group's choice
```

`holiday-settings --group-name prod` or `--name web-1` without a choice shows the current setting;
`status` shows the setting in effect for a node and where it comes from.

In the dashboard: the **Holidays** page; each group's page has a **Holidays** card (its upcoming
holidays, adding a date or range for that group, **Run on account-wide holidays**, and **Skip
tomorrow**); each node's page has an **Account-wide holidays** choice. A node whose day is a
holiday says so on its page. Holidays and these settings are included in backups and come back
with `restore`/`rebuild` (the settings are also kept in tags on each node's OS volume and in the
group's Object Storage record).

### 8.15 Recovering safely: self-healing, missed schedules and alerts

**Self-healing (`heal`).** The scheduler compares every node with your account every few minutes
and fixes its own records where the account proves them wrong — for example after a database was
restored from a snapshot taken before a node was started:

- the node's OS volume is attached to an instance its record doesn't point at → that instance is
  the node; it's recorded (never a second one created, and a `stop` stops that one) -- unless that
  instance has a disk of its own or boots from something else (the volume attached to another
  machine as a second disk, say): then it's left alone with a warning, and a `stop` never deletes
  an instance that isn't booting from the node's OS volume;
- the record points at an instance that's gone and nothing holds the OS volume → stopped;
- an instance with the node's label, no disks and none of your volumes, at least 15 minutes old →
  left behind by a start that never finished; it's deleted (nothing that can hold data ever is);
- a node marked "in progress" while no start/stop is running for it → cleared.

`start`/`stop` make the same check for the node they act on. Run it yourself any time:

```
linode-instance-scheduler heal                 # every node; --name to limit
```

Volumes and reserved IPs are never deleted by this.

**Missed schedules (`schedule-catch-up`).** The scheduler acts on a start or stop for an hour after
it was due. After a longer outage (for example while the scheduler host was being replaced), nodes
are left as they were until their next scheduled time. These are listed — on the dashboard's
Instances page and here — but nothing is started or stopped until you approve it:

```
linode-instance-scheduler schedule-catch-up                     # list
linode-instance-scheduler schedule-catch-up --apply             # act on them, after confirming
linode-instance-scheduler schedule-catch-up --apply --include-changed-by-hand --yes
linode-instance-scheduler schedule-catch-up --dismiss           # leave them as they are
```

A node someone started or stopped by hand after the scheduled time is marked as such and left
alone unless you include it. Stops run before starts, following each group's start order.
`--dismiss` (the card's "Leave as is") leaves the listed nodes as they are: they're no longer listed
or alerted on for that missed time, and follow their schedule again from its next start or stop.

A start or stop time from before a schedule was changed is not treated as missed, because the old
schedule was in force then. This covers a node's own schedule, its group's schedule, a change of
group membership and holidays. A node then changes state at the new schedule's next time. To bring
it in line sooner, start or stop it by hand.

**Finding out why (`diagnose`).** "Why didn't this node start at 21:00?" is answered from the
records, not guessed:

```
linode-instance-scheduler diagnose --name web-1
linode-instance-scheduler diagnose --group-name app
linode-instance-scheduler diagnose --text "3 nodes not in the state their schedule wants: web-1 ..."
linode-instance-scheduler changes --group-name app      # who changed what, before and after
```

It checks, in order: a schedule, membership or holiday change made after the time in question; a
change by hand; failed attempts (and why: SSH host key, the account's instance limit, an address
in use, capacity, a guardrail); the scheduler stopped or standing by across that time; a wait on the
start order; a holiday; and, when the Linode token can read them, changes made to the instance in
Cloud Manager -- including a node powered off or deleted there while this tool still records it as
running (it reads the instance's live state for that). Linode records the tool's own starts and
stops under the Linode user that owns its API token; `diagnose` matches each of those to the
tool's record of who asked for it (shown in its timeline as, for example, "Linode: delete of
instance 123 by instance-scheduler -- this product (stop by alice)"), and reports any change by
another Linode user, or by the token's user with no matching record, as made outside the tool. It names only a cause it can prove, with the evidence and the next step; otherwise
it says it can't tell and shows the timeline. In the dashboard, "Investigate" next to a node's or
group's "Why" line, and "Why?" on the off-schedule card, show the same, with buttons for the
suggested steps. Ask answers a "why" question about a named node, or a pasted alert, the same way.

**Alerts.** Set a webhook (`LIS_ALERT_WEBHOOK_URL` — Slack and Teams incoming webhooks work as is)
and/or email (`LIS_ALERT_EMAIL_TO` plus the `LIS_ALERT_SMTP_*` settings; see `DEPLOYMENT.md`
§8.2) to hear about: the scheduler not checking in for 10 minutes, backups failing, the deployment
SSH key not stored in every place, nodes staying off their schedule, or a node that keeps failing
to start or stop (three failures in a row with no success since). Each is sent when it
starts, every 6 hours while it lasts, and once when resolved. Linode blocks outbound SMTP ports on
new accounts until support opens them.

```
linode-instance-scheduler alerts-test        # send a test alert
linode-instance-scheduler alerts-check       # what needs attention now (exit 1 if anything)
```

Alerts also cover a failed daily restore test of the backups, the Linode API token being rejected
or expiring within 14 days, the disk holding the database nearly full, Shutdown Watchdog being
off on the scheduler's host, a pending deletion that's overdue or keeps failing, bucket versioning
that can't be turned on (the access key lacks permission to change bucket settings), a failed or
missing recovery drill (when drills are turned on), and the host's clock: more than 60 seconds off
Linode's API time, or the operating system reporting it isn't time-synchronized (fix with
`timedatectl set-ntp true`). Schedules, leases and backup ages all depend on the clock.

**Setting up email alerts.** On the dashboard's System backup page, fill in the Alerts form: who
to send to, your SMTP server, port, security, user and password, then **Save** and **Send test
alert**. Or from the command line:

```
linode-instance-scheduler alerts-config --email-to ops@example.com --smtp-host smtp.gmail.com \
    --smtp-port 587 --smtp-user you@example.com --smtp-password     # prompts for the password
linode-instance-scheduler alerts-test
```

Use the SMTP server of a mail account you already have (Google Workspace: `smtp.gmail.com`, port
587, with an app password; Microsoft 365: `smtp.office365.com`, 587) or a sending service
(SendGrid, Mailgun, Amazon SES, Brevo). Linode blocks outbound SMTP ports (25, 465, 587) on new
accounts until you ask support to open them; a service that accepts port 2525 works without that.
For a webhook, `--webhook-url` (Slack: an incoming-webhook URL; Teams: a workflow webhook URL).
The running services pick the change up without a restart.

**When the whole host is gone,** nothing on it can alert you. Linode's Shutdown Watchdog (turned on
by `install.sh`; `host-watchdog --enable`) boots the host again after a crash, and
`LIS_HEARTBEAT_URL` sends a ping every minute to an uptime monitor of your choice (Healthchecks.io,
Better Stack, Uptime Kuma), which tells you when the pings stop.

**One active scheduler.** With Object Storage, the active scheduler holds a lease in the bucket. A
second scheduler (an old host coming back after you recovered onto a new one) finds the lease and
stands by -- it starts, stops and uploads nothing, and alerts. `scheduler-lease` shows who's
active; `scheduler-lease --take-over` (or the System backup page) moves it, only once you're sure
the other host is gone.

A standby host also refuses changes made on it: its API answers `409` to anything other than a
read (and the take-over itself), and the CLI refuses commands that would change nodes, schedules
or settings, pointing at the active host. Two hosts changing the same nodes could undo each other.
Set `LIS_ALLOW_STANDBY_CHANGES=1` to override (for example while you know the active host is gone
and you haven't taken over yet). If the bucket can't be reached, the scheduler that was active
keeps scheduling, and a host that wasn't active stays standby — it can't tell whether the other one
is still running; `scheduler-lease --take-over` overrides that too.

**Restore testing.** Every day the newest snapshot is downloaded and checked, and the stored key
copies decrypted, without touching the live database (`backup-verify` on demand). A damaged
database is never uploaded. Upgrading with `install.sh install` takes a snapshot first, in
`backups/pre-upgrade-<time>/`.

**Consistency check (`doctor`).** Everything this tool needs to recover a node lives in three
places: its database, tags on your Linode resources, and (if configured) Object Storage. `doctor`
checks that they agree:

- every node's OS volume, data volumes and reserved IP still carry exactly its name and role tags,
  and no resource is tagged for two names;
- no two nodes are recorded with the same VPC address;
- a running node's recorded instance exists and holds its OS volume, and nothing holds a stopped
  node's OS volume;
- no node is marked as starting or stopping when nothing is running;
- the recovery tags on each OS volume (schedule, group, start order, hooks, manual-only, holiday
  setting) match the node's current settings;
- the Object Storage copies of nodes, groups, pending deletions, API tokens, holidays and
  pre-migration backups match the database (when Object Storage is configured);
- every running node's SSH address has a trusted host key.

It only reads (it never changes anything and never connects to your nodes), takes a few seconds,
and prints each problem with how to fix it. The active scheduler runs it once a day; any problem
is alerted like the other conditions above (when it starts, every six hours while it lasts, once
when it clears), and the next clean run — from the scheduler, the command line or the dashboard's
System page — clears it. Run it after anything unusual: an outage, hand changes in Cloud Manager,
a restore.

```
linode-instance-scheduler doctor          # exit 0 consistent, 1 problems found, 2 a check couldn't run
linode-instance-scheduler doctor --json   # for scripts and monitoring
```

Some findings are **notices**, shown but never failing the check: resources tagged for a node this
database doesn't know (normal when another deployment of this tool shares the account; otherwise
run `rebuild`), an Object Storage copy kept for a node you removed with `deregister`, and a stopped
node's VPC address taken by another instance (the node moves to a free address at its next start).

**Recovery drills.** A restore test checks the backups; a drill checks that a new host could
actually take over. It creates a throwaway `g6-nanode-1` (tagged `lis-recovery-drill`), copies the
release copy from your bucket and a minimal `.env` onto it, and runs `install.sh recover --drill`
there: the latest snapshot and the deployment key are restored, then it checks that every node's
volumes and reserved IP exist and that running nodes can be reached over SSH with the restored key.
The drill host is read-only — it takes no lease, doesn't rebuild or heal, uploads nothing and starts
no services — and the VM is always deleted afterwards. It costs one nanode for about 10–15 minutes.

```
linode-instance-scheduler recovery-drill                     # now (this host's region)
linode-instance-scheduler recovery-drill --region us-ord     # another region
linode-instance-scheduler recovery-drill --keep-vm           # leave the VM to inspect (delete it yourself)
```

Set `LIS_RECOVERY_DRILL_DAYS=7` and the scheduler runs one weekly; an alert is sent when the last
drill failed or none ran recently. Over the API: `GET /system/recovery-drill` (the last result) and
`POST /system/recovery-drill` (run one now).

**Rotating credentials.** If a credential may have leaked, or on your own schedule:

```
linode-instance-scheduler rotate-credentials api-token      # the Linode API token
linode-instance-scheduler rotate-credentials bucket-key     # the Object Storage access key
linode-instance-scheduler rotate-credentials oauth-secret   # the dashboard's OAuth client secret
linode-instance-scheduler rotate-credentials ssh-key        # the deployment SSH key on every node
linode-instance-scheduler rotate-credentials ssh-key --continue   # move nodes not yet moved
```

- `api-token`: a new token with the same scopes (and the same lifetime, if the old one expires) is
  written to `.env` and picked up by the running services without a restart; the old token is
  revoked only after the scheduler has switched to the new one.
- `bucket-key`: a new key with the same bucket access is created and tested, written to `.env`,
  then the old key is deleted.
- `oauth-secret`: the secret is reset and the new one written to `.env`; the old one stops working
  at once.
- `ssh-key`: a new deployment key is made. The old one is kept — encrypted in the database and the
  bucket, and offered by every SSH connection alongside the new one (`LINODE_SSH_PREVIOUS_KEY_PATH`)
  — until every node trusts the new key. Running nodes move at once: the new key is added, a login
  with only the new key is proven, then the old key is removed. Stopped nodes move after their next
  start; the scheduler does this, or run `--continue`. A node needing manual recovery is never started by the tool, so it can't move by itself: the old key stays trusted until it is recovered (`onboard --force` works during the rotation, since both keys are offered) or offboarded. A node that leaves the registry during the rotation (for example `deregister`) keeps it open while its volumes are still tagged on the account, because `rebuild` could bring it back still trusting only the old key; bring it back and start it, or offboard it. A node added to the registry during the rotation joins it. The rotation reports these nodes separately and raises an alert, as it does when a node keeps failing to move or the rotation has been open for a week. If you restore onto a new host part way
  through, `ssh-key-restore` brings back the previous key too. If you use the assisted migration
  copy (§6.6), add the new public key to your profile's Lish keys yourself afterwards.

Over the API: `POST /system/credentials/rotate` with `{"kind": "ssh-key", "continue_rotation":
false}`, and `GET /system/credentials/rotation` for a key rotation's progress.

### 8.16 LKE node pools — schedule a Kubernetes node pool's size

A node pool of a Linode Kubernetes Engine (LKE) cluster can be scheduled exactly like an instance:
the same schedules, groups, holidays, start order, manual starts and extensions apply.

```
linode-instance-scheduler pool-register --name ci-runners --cluster-id 667901 --pool-id 990573
linode-instance-scheduler group-add --name ci-runners --group-name office-hours
```

(The cluster and pool IDs are in Cloud Manager under Kubernetes, or from
`linode-cli lke pools-list <cluster-id>`.)

Linode doesn't allow a node pool of size 0, and a cluster must always keep one pool. So:

- **Stop** deletes the pool when the cluster has another pool and the pool's settings have just
  been saved to Object Storage — its nodes are removed and stop billing. If it's the cluster's only pool, stop scales it to one node instead (and turns its
  autoscaler off). `pool-set-stop-mode --stop-mode delete|scale-down|auto` fixes the choice;
  `auto` (the default) picks as described.
- **Start** brings the pool back to the size it had when it was stopped, with the same node type,
  labels, taints, tags and autoscaler settings (and, where the pool has them, its Cloud Firewall,
  update strategy and pool label; if Linode no longer accepts one of these, the pool comes back
  without it and a warning says so). A recreated pool runs the cluster's current Kubernetes version. A deleted pool comes back as a new pool with a new
  pool ID; Kubernetes schedules your workloads onto its nodes as they become ready. A start counts
  as done once every node is ready (up to 15 minutes; otherwise the pool is marked `unreachable`
  and the next start finishes the job).
- Nodes are disposable to Kubernetes, so nothing on a node's own disk is kept across a stop. Keep
  data in persistent volumes.
- **Never register a pool this tool itself can run on.** If you run the tool on LKE, a stop of
  the pool hosting its own pod would remove the scheduler mid-stop, and nothing would be left
  running to start the pool again. Keep the tool on a pool (or cluster) you don't schedule.

A pool has no OS volume to carry recovery tags, so its settings (pool spec, schedule, group) are
kept in its Object Storage record; while running, the pool also carries the tag
`lis-pool:<name>`. A stop that scales a pool to one node adds `lis-pool-stopped:<name>` in the same
request (the start removes it), so the tool can tell a stopped one-node pool from a running one;
leave both tags in place. After a total loss of the local database, `rebuild` brings a pool back from
Object Storage — so configure system backups before scheduling pools. Commands that only make
sense for an instance (`offboard`, hooks, `set-vpc-address`, `reset-host-key`, migration) refuse
a pool, and a group's hooks run only on its instance members, never on its pools (there is no
machine to run them on — in Kubernetes, use a DaemonSet or a readiness gate instead). Without
Object Storage, `auto` scales the pool to one node instead of deleting it (its settings would
otherwise exist only in the local database), and `delete` mode is refused; to stop scheduling one, run `deregister --name <name>`. The pool itself is left as it is; its Object Storage record is kept but marked deregistered, so `rebuild` doesn't bring it back. If the pool was stopped (deleted) at that moment, `deregister` refuses unless that record exists, since it would hold the only copy of the pool's settings; if it was stopped by scaling it to one node, `deregister` refuses too (nothing would scale it back) — start it first. A pool that already runs a single node with no autoscaler and can't be deleted (the cluster's only pool) saves nothing when stopped; it is just recorded stopped. The pool keeps its `lis-pool:<name>` tag; remove the tag in Cloud Manager before registering the same pool under a different name.

### 8.17 Getting help on the host: `doctor --fix` and `explain`

Two built-in helpers, both working on this host with nothing sent anywhere. Neither
changes anything without you confirming, except the small set of automatic repairs below.

**Repairs (`doctor --fix`).** `doctor` lists what doesn't agree; `--fix` turns that into a plan,
shows it, asks once, applies it and checks again:

```
linode-instance-scheduler doctor --fix              # plan, confirm, apply, re-check
linode-instance-scheduler doctor --fix --only D7    # just one kind of finding
linode-instance-scheduler doctor --fix --name web-1 # just one node
```

The repairs only bring the copies back in line with the database or reconcile a node with what's
really on your account: re-upload an Object Storage record, rewrite a node's tags, clear an
in-progress mark that nothing holds, reconcile a node's instance (`heal`), move a stopped node off
a VPC address another instance took. The first three are also applied automatically by the active
scheduler after its daily check (set `LIS_AUTOFIX=off` in `.env` to stop that); the others always
wait for a person. Nothing that holds data is ever deleted (the only thing a repair deletes is a
blank leftover instance from an interrupted start, with no disks and none of your volumes, the
same as `heal` does), no customer data is touched, and a finding only a
person can judge (a resource claimed by two names, a missing resource) is listed, never repaired.
The dashboard's System backup page has the same in its "Consistency check" card ("Review fixes").

**What does this error mean? (`explain`).** Paste an error from anywhere -- the dashboard, the
CLI, a log, the installer -- and get its cause in plain words, read-only checks against this
deployment (the node's state, its trusted host key, who holds its VPC address, the API token...)
and what to do, with the safe repair when there is one:

```
linode-instance-scheduler explain "[400] Linode busy."
linode-instance-scheduler explain --file install.log
linode-instance-scheduler explain --name web-1      # that node's last failure
```

Every error banner in the dashboard has an "Explain this" link.

**Questions about the product (`ask`, and Ask on the dashboard).** Ask a question in your own words
and get the matching part of the product's own reviewed answers:

```
linode-instance-scheduler ask "how do I move a node to another group"
```

Ask works like a search of the product's help. When it is very sure, it answers directly; otherwise
it says it isn't sure and lists up to five answers that may help, which you open with a click (or
`ask --answer <id>`). It never writes answers of its own -- every answer it shows was written and
reviewed as part of the product -- and a pasted error goes to `explain` above. It runs on this host
with a small language model shipped with the release (on the CPU, nothing sent anywhere). Questions
about other software ("upgrade postgres") get suggestions at most, never a direct answer.
`LIS_ENABLE_ASK=0` in `.env` turns Ask off.

**Smart Ask.** When the instant answer isn't sure, an open model running on this host can read the
few closest answers and pick the one that fits, using this deployment's own facts. It only chooses
among the product's reviewed answers -- it never writes one -- and nothing leaves the host. Its pick is
listed first among "These may help", marked as the model's pick with its one-line reason, rather
than shown as the answer: on questions the instant search isn't sure about, a model's pick is
wrong too often to present as certain, so you open the one that fits. The dashboard waits for the model when the instant
answer isn't sure (about 5-10 seconds on an 8-vCPU host; "Show suggestions now" skips the wait). `install.sh`
sets it up when the host has the memory: 16 GB or more gets Qwen 2.5 7B, 8 GB gets Qwen3 4B, a
smaller host leaves it off (Ask still works). The model runs as its own service
(`linode-scheduler-ollama`) at low priority, so the scheduler and the API always come first, and
answers one question at a time. `sudo ./install.sh install --no-smart-ask` turns it off (and stops
that service); `ask-llm status` shows whether the model answers and warns when the host is too
small. To use a model server you run elsewhere, set `LIS_ASK_LLM_URL` and `LIS_ASK_LLM_MODEL`
yourself; the installer leaves your own server alone.

### 8.18 Budgets and guardrails — limits on manual starts and extensions

Schedules save money only as long as manual starts and extensions don't eat the savings. Budgets
and guardrails limit those; they never stop a scheduled start.

**Budgets.** A budget is a number of running hours per calendar month (UTC) for a schedule group
(its members' hours added up) or one node, counted from the start/stop history:

```
linode-instance-scheduler budget-set --group-name web --hours 300 --action block-manual
linode-instance-scheduler budget-list
```

At 80% and at 100% an alert is recorded (and sent, if alerts are set up). At 100% the budget's
action applies: `alert` (nothing more, the default), `block-manual` (manual and API starts and
extensions are refused; scheduled starts still run) or `stop-after-extension` (no new
extensions; extensions already running end normally).

**Guardrails.** Set for all instances, and optionally per schedule group; all off until set. A
group's own setting replaces the all-instances one for that group's nodes, setting by setting
(`--group-name web`); `--clear` on a group's setting goes back to the all-instances one. A group's
`max_running` counts only that group's nodes, and the all-instances `max_running` still limits the
whole fleet:

| Guardrail | Example | Effect |
|---|---|---|
| `max_extension_hours` | `guardrail-set --key max_extension_hours --value 4` | an extension longer than this is refused |
| `max_running` | `guardrail-set --key max_running --value 20` | a manual start is refused while this many nodes are already running |
| `quiet_periods` | `guardrail-set --key quiet_periods --value '{"timezone": "Asia/Kolkata", "rules": [{"days_of_week": ["sat", "sun"], "start_time": "00:00", "stop_time": "23:59"}]}'` | nothing may be started or extended by hand during these times (same rule format as a schedule) |
| `safety_net` | `guardrail-set --key safety_net --value true` | once a night, nodes that have a schedule and are running outside it, with no running extension or approval, are stopped (each stop is recorded as a warning in Activity). It runs at `safety_net_hour` (default 2, i.e. 02:00) in each node's own schedule timezone -- for a group's nodes, the group's timezone -- so `--group-name web --key safety_net_hour --value 3` means 03:00 where group web's schedule runs |

`--clear` removes one. On the dashboard, the Guardrails card's **Applies to** list switches between
All instances and each group.

**Approvals.** A refused start or extension can be requested — `approval-request --name web-1
--action start --reason "release night"`, or **Request approval** under the error on the
dashboard. An admin sees the requests on **Budgets** and approves them (for 4 hours by default)
or denies them; `approval-grant` approves one directly. While approved, the action goes through.
On the host, `start`/`extend --override-budget` goes through anyway and is recorded in Activity.
A refusal exits with code 6 (409 over the API, with `guardrail_rule` in the response).

Budgets and guardrails are kept in Object Storage when it is configured, and come back with
`restore` and `rebuild`. API token scopes: `budgets:read` (in `read`), `budgets:write` (in
`configure`), `approvals:request` (in `operate`) and `approvals:decide` (admin only).

### 8.19 Health and cost insight

The tool keeps 30 days of its own health figures in its database: how long starts and stops
take, Linode API latency, errors and rate-limited (429) responses, how long each scheduler check
takes, database lock waits, and the host's memory, load and free disk. `insight` (or **Health** on
the dashboard) shows them, and lists **findings** — rate limiting, API errors or slowness, starts
much slower than over the last week, slow scheduler checks, a busy database, low memory — each
with the likely cause and the fix. Findings are also health alerts, sent like the others.
`insight --prometheus`, or `GET /metrics` with a token holding `system:read`, gives the same
numbers in Prometheus format for Prometheus/Grafana.

**Cost insight.** While a node runs, the scheduler reads its CPU from Linode's own statistics once
an hour (nothing is installed on the node). A node that averaged under 5% CPU with peaks under
30% while running over the last two weeks (at least about a day of running time in that period)
gets a right-sizing hint ("a smaller plan would likely do").
Each instance's page shows its CPU.

**Server metrics (optional, off by default).** For memory, root disk space and load from inside
the node, turn on server metrics for a node or a whole group (`metrics-agent --name web-1`,
`metrics-agent --group-name web`, or **Turn on server metrics** on the instance page; a node's own
setting overrides its group's). The scheduler then installs the standard Prometheus
node_exporter on running nodes (needs systemd, x86_64 or arm64, and one download from github.com
checked against a pinned checksum), listening on the node's loopback address only — no port is
opened — and reads it over the SSH access it already has, every 5 minutes. `--disable` removes it
again. Turning it on or off needs the admin-only API scope `metrics-agent:manage`, because it
installs a program as root.

### 8.20 Usage-learned schedules

Schedules are often a guess ("9 to 6 on weekdays") while real use is narrower. While a node runs,
the scheduler keeps its CPU and outbound network per half-hour (from Linode's own statistics), and
`schedule-suggest --name web-1` (or **Suggested schedule** on the instance or group page) turns the
last 4 weeks into a suggestion:

```
Suggested: mon,tue,wed,thu,fri 09:30-18:30 -- saves 21 points more than today's schedule.
Covers all activity seen in the last 26 days.
```

For each weekday it takes the first and last half-hour with real activity (CPU or network clearly
above the node's own idle level, or a manual start/extension), adds 30 minutes before and after —
more before when the node has a post-start check, so it is ready in time — and turns a weekday with
no activity off. A group's suggestion covers every member. It never suggests a schedule that would
have stopped a node during activity it saw, and it needs usage from at least 14 different days
first. Only hours the node actually ran can be seen, so a suggestion mostly narrows a schedule; the
margins, and manual starts or extensions counting as activity, can make it reach past today's
hours.

Nothing changes until you apply it (`--apply`, or **Apply suggestion**), which goes through the
normal schedule setting (so the recovery copies follow); `--undo` (or **Undo last applied**) puts
the previous schedule back.

### 8.21 Wake on demand

A node can stay stopped — costing no compute — until someone actually needs it, and stop again once
it has been idle:

```
linode-instance-scheduler wake-policy --name dev-1 --mode wake --idle-minutes 30
linode-instance-scheduler wake-link --name dev-1
```

Policies, per node or group (a node's own wins):

- `wake` — **no schedule**: the node or group stays stopped until someone wakes it, and stops again
  when idle. Choosing it while the node's or group's own schedule is on turns that schedule off
  (its rules are kept): the dashboard asks you to type the name to confirm, and the command line
  and API need `--disable-schedule` / `"disable_schedule": true`, refusing otherwise. While a
  node or group is set to `wake`, its schedule can't be turned back on; switch to `schedule-wake`
  or `off` first. A node that follows its group's schedule can't be set to `wake` on its own (set
  the group, or remove the node from it), and a node set to `wake` can't join a group whose
  schedule is on. Members of a `wake` group that have their own schedule keep it.
- `schedule-wake` — the schedule runs as usual; a wake outside its hours is stopped when idle.
- `off` — no waking.

Inside an active schedule's hours the schedule decides when a node stops, never the idle stop; and
a wake ends when the node is next stopped by any means, so its next start (by its schedule or by
hand) is never treated as a wake.

**Waking it.** Signed-in people use **Wake now** on the instance page or `wake --name dev-1` (their
normal start permission applies). The node's **wake link** — created with `wake-link` or **Create
wake link**, shown once — opens a page that says the node is stopped and starts it when the visitor
presses **Start it** (opening the link alone never starts anything, so link previews and bots
don't), shows progress, and then shows the address to connect to. Traffic never goes through this
tool: once the node is up, people connect to it directly, so use its reserved IP or a DNS name for
it as usual. A new link replaces the old one; `wake-link --remove` turns it off. A link can't start
the same node again within 5 minutes. Budgets and guardrails (§8.18) apply to wakes like to any
manual start.

**Idle stop.** Every 5 minutes the scheduler checks each woken node: if Linode's own CPU and
outbound network figures stayed near idle and nobody has an SSH session open for the policy's idle
minutes (default 30) since it was woken or last busy, it is stopped through the normal stop (hooks,
recovery copies, history, recorded as `wake-idle`). If the check can't tell (statistics or SSH
unavailable), the node keeps running. "Near idle" means CPU below 15% (Linode's figure includes I/O
wait, which sits around 5-10% on an idle node booted from a volume), outbound traffic below
100,000 bits/s and, for a node woken through a NodeBalancer, no two readings in a row (10 minutes)
above 5 connections/s on that NodeBalancer (a public one sees short bursts of stray internet traffic). Change these with
`LIS_WAKE_IDLE_CPU_PCT`, `LIS_WAKE_IDLE_NET_BPS` and `LIS_WAKE_NB_BUSY_CONNECTIONS` in `.env`.
Linode's figures come as 5-minute samples and the newest ones are filled in late, so a node counts
as in use until 15 minutes after its last busy sample ends: a node stops about 15 minutes plus its
idle minutes after the last real use (an open SSH terminal session counts up to the last 5-minute check that still sees it).
Activity says why a woken node is being kept running (in use, and by what), when it stops being
used ("not in use since 10:15; it stops at about 10:45 unless used"), and when a woken
node is inside its schedule's hours and left to the schedule -- each once, when it changes. `why
--name` gives the same answer on demand, with the earliest idle stop time. A woken node doesn't get the 2-hour manual-start timer, and
the nightly safety net leaves it alone. Policies and wake links are kept in Object Storage and come
back with `restore` and `rebuild`.

**Waking a group.** A group's policy applies to every member without its own. **Wake the group
now** on the group page, or `wake --group-name app`, starts the groups it depends on first, deepest
first (members already running are skipped), then the group; if a stage doesn't come up, the chain
stops there. Waking a single node that's in a group also starts that group's dependencies first.
Each member started is marked woken, so its idle stop applies; a dependency group stops when idle
only if it has wake on demand on itself, otherwise its own schedule stops it.

**Waking through a NodeBalancer.** Visitors to a site's normal address can wake its node or group.
Attach your NodeBalancer's ports to it -- every port visitors use, such as both 80 and 443 (each
port is attached on its own; the dashboard lets you tick several at once): this scheduler host
joins each port as a **backup** node, which the NodeBalancer uses only while every real backend fails its health check — that is,
while the node or group is stopped. Visitors then see a "the service is starting" page (a progress bar with the percentage done, the current step, the time it has taken so far and a usual-time estimate, plus a tiny game to pass the wait; it names no servers and loads nothing from elsewhere), which
starts it when someone presses **Start it** (the default) or on any request (`--trigger any`, at
most once per cooldown, 15 minutes by default — bots, crawlers and internet scanners count as
visitors too, and a public NodeBalancer gets them all day, so `any` suits a private address best;
Activity records each wake's visitor address, the page asked for and its browser; within the
cooldown the page offers the Start button instead, so a person can still start it). The
NodeBalancer's own health checks never start anything, and the page names nothing. As soon as the
real backends pass their health checks again, the NodeBalancer sends traffic to them by itself:

```
linode-instance-scheduler wake-policy --group-name app --mode wake --idle-minutes 30
linode-instance-scheduler wake-nodebalancer --show 203.0.113.7
linode-instance-scheduler wake-nodebalancer --group-name app --nodebalancer 203.0.113.7 --port 80
linode-instance-scheduler wake-nodebalancer --group-name app --nodebalancer 203.0.113.7 --port 443
```

or the **Wake through a NodeBalancer** part of the instance's or group's Wake card. Requirements:

- a **VPC NodeBalancer** in the same region as this scheduler, with this scheduler host on the
  NodeBalancer's VPC subnet (the backends are reached by VPC address, which the scheduler keeps
  across stop and start; backends on 192.168 private addresses aren't supported);
- an **HTTP** port, or **HTTPS terminated at the NodeBalancer** (TCP passthrough isn't supported),
  with a health check — best an HTTP check of a path your app answers only when it's ready;
- the NodeBalancer able to reach this host on TCP 19200–19499 (each attached port gets its own
  page port from that range; the scheduler (`poll`) service serves the page). This is set up for
  you: if the scheduler host has a Cloud Firewall that drops inbound traffic by default, the tool
  adds one rule to it, `lis-wake-pages`, allowing TCP 19200–19499 from the NodeBalancers' VPC
  subnets, keeps it in place, and removes it when nothing wakes through a NodeBalancer any more.
  Nothing else in the firewall is changed. If that firewall also protects other servers, the rule
  applies to them too, and Activity says so. The API token needs **Firewalls** (Read/Write) for
  this; set `LIS_WAKE_MANAGE_FIREWALL=0` in `.env` to manage the firewall yourself. `install.sh`
  opens the same range to private (VPC) addresses in the host's own ufw or firewalld. On a
  replacement host the rule is added again by itself.

  `wake-nodebalancer --show <IP>` lists this host's backup node on each port. **DOWN** there means
  the NodeBalancer can't reach the page, so visitors can't wake anything; this is also reported
  as a health alert (`wake-page`).

The managed nodes behind the NodeBalancer are added back to it after every start, as for any node
(see §8.24). A woken node also counts NodeBalancer connections as use, so it isn't stopped while
visitors are on the site.

Everything is recoverable: the attachments and remembered backends are in Object Storage and on the
NodeBalancer itself (tags `lis-wake:…` and `lis-wake-be:…`, and our backup node labelled
`lis-wake-…`), so `restore` and `rebuild` bring them back. On a replacement host, the scheduler
checks every few minutes that each port has exactly one backup node of ours pointing at this host,
adds it if it's missing and removes ours that still point at the old host. `wake-nodebalancer
--name … --remove` (or **Remove**) detaches it. API scope: `wake:manage`.

### 8.22 Slack and Microsoft Teams

**Notifications.** Every health alert, every approval request (with Approve/Deny buttons in Slack)
and, if turned on, a daily summary at 03:00 UTC go to chat:

| Setting (.env) | What |
|---|---|
| `LIS_SLACK_BOT_TOKEN` | The Slack app's bot token (`xoxb-…`), scopes `chat:write` and `commands` |
| `LIS_SLACK_CHANNEL` | Channel ID for notifications (invite the bot to it) |
| `LIS_SLACK_APP_TOKEN` | An app-level token (`xapp-…`, scope `connections:write`) to receive commands over Socket Mode |
| `LIS_TEAMS_WEBHOOK_URL` | A Teams incoming webhook (Workflows) URL for notifications |
| `LIS_TEAMS_OUTGOING_SECRET` | The security token of a Teams outgoing webhook, to receive commands |
| `LIS_CHAT_DAILY_SUMMARY` | `1` for the daily summary (off by default) |

Changes in `.env` take effect within a minute, without a restart. **Send test message** on Users &
teams checks the setup.

**Slack setup.** Create a Slack app, turn on Socket Mode (this creates the `xapp-` token), add the
slash command `/scheduler`, turn on Interactivity, add the bot scopes `chat:write` and `commands`,
install it to the workspace and invite the bot to the notification channel. Socket Mode is an
outbound connection from the API server to Slack, so nothing has to be reachable from the internet.

**Teams setup.** For notifications, create an incoming webhook (Workflows) for the channel. For
commands, create an outgoing webhook pointing at `https://<your dashboard>/chat/teams` (this needs
the dashboard's HTTPS address, e.g. `install.sh --domain`) and put its security token in
`LIS_TEAMS_OUTGOING_SECRET`; requests are checked against it.

**Commands** (`/scheduler <command>` in Slack, `@Scheduler <command>` in Teams): `status [node|group]`,
`savings [node]`, `why <node>` (explains the node's last failure), `start <node>`, `stop <node>`,
`extend <node> [hours]`, `approvals`, `approve <id>`, `deny <id>`, `link`, `help`. Starting and
stopping run in the background and the result is posted when done.

**Who may do what.** Anyone in the channel can read. To act, a person runs `link`; the reply holds
a one-time dashboard link (valid 15 minutes) which, opened while signed in, links their chat user to
their dashboard user. Their dashboard role and teams (§8.8) then apply exactly as on the dashboard,
and budgets and guardrails (§8.18) too. Every chat action is recorded in Activity with both the chat
user and the dashboard user; admins see and remove links on Users & teams.

### 8.23 Hibernate a node stopped for a long time

A stopped node still pays for its Block Storage volumes. For a node you won't need for weeks or
months, `hibernate` moves its data to Object Storage, which costs much less per GB (and the copy is
compressed and encrypted: each hibernation gets its own random key, every chunk is encrypted on the
helper VM before upload, and the key is stored only sealed with the deployment passphrase, which
`hibernate` and `unhibernate` therefore need):

```
linode-instance-scheduler hibernate --name old-1 --estimate   # what would move, and how long
linode-instance-scheduler hibernate --name old-1 --helper-type g6-standard-4   # type the name to confirm
linode-instance-scheduler hibernate --name old-1 --status     # progress while it runs
linode-instance-scheduler hibernate --name old-1 --pause      # pause; run the command again to resume
linode-instance-scheduler hibernate --name old-1 --cancel     # give it up (type the name to confirm)
linode-instance-scheduler unhibernate --name old-1            # bring it back (stopped)
```

The node must be stopped and Object Storage configured. A helper VM is created in the node's region
with the node's volumes attached. You choose its type (`--helper-type`, or the dashboard's list; the
default is the smallest, `g6-nanode-1`): the helper runs two copy workers per vCPU, so a bigger one
finishes sooner and costs more per hour while it runs. Above about 8 vCPUs Object Storage sets the
pace, so bigger types gain little. `--estimate` (and the dashboard) shows, for your volumes, the time
each type takes and what its helper VM costs (a one-time cost: the helper is deleted when the copy is
done). Both also compare, at your region's Linode prices and before compression, what the data costs
per month on Block Storage and on Object Storage, and the network transfer each copy can use:
hibernating uploads every chunk and downloads it again to check it (about twice the data), and
unhibernating downloads it once. Transfer is taken first from your account's monthly network transfer
pool and charged only past it. As a guide, 1 TB takes about 3.5-7 hours on `g6-nanode-1`
and about 1.5-2 hours on `g6-standard-4`; empty space copies faster than data that doesn't compress.

Each volume is read in 512 MiB chunks; each chunk's checksum is recorded, it is compressed with
zstd (a chunk that doesn't get smaller, such as already-compressed data, is stored as it is),
encrypted and uploaded, then **downloaded again and checked**. With `--fast-check` (or the dashboard's
"Fast check" box), a chunk is instead checked on the helper -- the packed chunk is decrypted and
decompressed and its checksum compared with the original -- and by its upload checksum (the MD5
Object Storage reports for what it stored, compared with what was sent). That avoids downloading
every chunk again, so it uses about half the transfer; it is at most a little faster, since the copy
is limited by the upload and the helper VM's CPU. It no longer proves each chunk can be read back
right away, so it isn't recommended unless transfer cost matters; the estimate shows both times
and transfers. Only when every chunk has been
checked is the node marked hibernated; its volumes are kept for the deletion grace period (7 days by
default, `LIS_DELETION_GRACE_DAYS`), so `unhibernate` within it simply reuses them. After that the
scheduler deletes them -- all of a node's volumes or none, each re-checked first (unattached, still
this node's). After that, `unhibernate` creates volumes of the same size and label (so the node's own
`/etc/fstab` still matches), writes each chunk back, checks it again on the volume, and leaves the
node stopped, ready to start. It also takes a helper type. The helper VM is deleted afterwards, and
the bucket keys never leave this host: the helper gets upload/download links that expire after two
hours, handed out a few at a time as the copy goes, so a copy can run for as long as it needs. The
reserved IP is kept, so the node comes back at the same address. Once a node is unhibernated, its
copy in Object Storage is kept for the same grace period (in case the restored node turns out wrong),
then the scheduler deletes it.

**Hibernating again after an unhibernate.** The copy an unhibernate leaves is used again. If the node
hasn't run since and its volumes weren't attached anywhere (checked against Linode's own events), the
copy is still exact and becomes the hibernation again at once -- no helper VM, nothing copied. If the
node ran, its disks have changed: the card and `--estimate` say *"There's a copy from <date>, but the
node has run since then, so its disks have changed. A new copy is needed"*, and only the chunks that
changed are uploaded (with the same encryption key); the unchanged ones point at the earlier copy,
each checked to still be in Object Storage before the new manifest is written. `hibernate --fresh`
(or the *Make a fresh, full copy* box) copies everything instead. An earlier copy's folder is never
deleted while a hibernation, or a hibernate in progress, still uses it.

Both waits are listed on the dashboard's **Pending deletion** page: a hibernated node's kept volumes
("hibernated volumes") and an unhibernated node's copy ("hibernate copy in Object Storage"), each with
its deletion date and a **Delete now** button (type the node's name) to stop paying for it early. The
same checks apply as for the scheduler's own deletion: volumes attached somewhere or claimed by another
name are kept, and a copy that a hibernated node still uses can't be deleted. Once a node is
hibernated, its Hibernate card shows **where the copy is** -- the bucket and its endpoint, the folder
`hibernate/<node>/<run>/` and the manifest `hibernate/<node>/manifest.json` (`hibernate --status`
prints the same); the chunks there are encrypted with the deployment passphrase.

**Progress, interruptions and retries.** While a copy runs, the instance page shows how far it got
(chunks, GB, speed, time left; `--status` prints the same), and the Activity log notes every 10%.
Every checked chunk is recorded, so nothing is copied twice:

- A chunk that fails (a network error, an expired link) is handed out again with fresh links; only
  a chunk that fails six times stops the copy.
- If the scheduler or API restarts, or the host reboots, the active scheduler carries the copy on
  by itself from the last checked chunk.
- **Pause** (the instance page, or `--pause`) stops a running copy: no more chunks are handed out,
  the ones in progress finish (a few minutes at most), and the helper VM is deleted so it stops
  billing. The checked chunks are kept. **Resume** (or the same command again) carries on from there
  with a new helper. While paused, the node's volumes (for an unhibernate, also the new volumes) and
  the copy made so far in Object Storage are still billed.
- If the copy stops by itself (a chunk keeps failing, the helper is lost), the helper is deleted the
  same way and the copy waits, paused, for Resume or Cancel.
- **Cancel** (`--cancel`, type the name to confirm) gives a copy up, also while it runs (it stops at
  its next check, like Pause, then cancels). Cancelling a hibernate leaves the node with its
  volumes, as it was -- hibernating only ever reads them -- and the partial copy is deleted after the
  grace period. Cancelling an unhibernate deletes the volumes it created; the node stays hibernated
  with its copy untouched.
- While a hibernate or unhibernate is in progress -- running, paused or stopped -- the node can't be
  started (by hand, by its schedule or by wake on demand) or offboarded: starting it would change
  disks part of which are already copied. Finish or cancel the copy first.

A hibernated node is never started by the scheduler and can't be started or offboarded until it is
unhibernated. Hibernating is never automatic: the instance page shows it only for stopped nodes, it
needs the name typed, and nodes stopped for 30 days or more are listed as suggestions. The copy's
manifest is kept in Object Storage, so `rebuild` and `restore` know a node is hibernated and where
its data is. API scope: `instances:hibernate` (admin only).

### 8.24 NodeBalancer membership — nodes rejoin their NodeBalancer after every start

When an instance is deleted, which every stop does, Linode removes it from its NodeBalancers if it
uses configuration-profile interfaces; with Linode Interfaces it leaves the entry in place, pointing
at a VPC address that is now free and could be given to another instance. The tool removes such a
left-behind entry right after the stop, so both kinds behave the same. So for **VPC NodeBalancers** the tool keeps a node's membership across stop and start, with
nothing to set up and with or without wake on demand: three app servers behind a NodeBalancer,
stopped every night and started every morning, rejoin it every morning.

Before each stop (before the pre-stop hook) it looks through your NodeBalancers for nodes at the
node's VPC address and records each one: the node's label, IP address and port, mode, weight and
VPC subnet, plus the NodeBalancer's label, address and region and the port's settings at that
moment (protocol, algorithm, session stickiness, health check type, path, interval, timeout,
attempts and expected body, passive checks, proxy protocol; never certificates or keys). Right
after the next start, whatever started it, each node is added back with the same label, mode and
weight; if that fails, the scheduler retries within 5 minutes. If the node's VPC address was moved
while it was stopped, the entry follows it. The port and its settings belong to the NodeBalancer
and aren't touched by a stop, so they are kept for reference: the list shows which settings were
changed on the NodeBalancer since. Other backends are never touched.

If you remove a node from a NodeBalancer while it runs, it isn't put back at its next stop.
`nb-membership --name <name>` lists a node's memberships (`--json` for scripts), and
`nb-membership --name <name> --forget [--nodebalancer-id N [--config-id M]]` stops adding one
back; on the dashboard, the instance page's **NodeBalancer membership** card does the same. API:
`GET`/`DELETE /instances/{name}/nodebalancer-membership` (scopes `instances:status`,
`instances:vpc-address`).

A NodeBalancer that reaches a node by its 192.168 private IP can't be kept: that address is new each
time the node starts. The stop warns about it; use a VPC NodeBalancer. If the NodeBalancers can't be
read before a stop (a Linode API problem, or a token without NodeBalancer access), the stop is
refused and nothing changes (a token without NodeBalancer access only warns: give it NodeBalancers Read/Write to keep memberships); `stop --force` stops anyway and loses any membership not already
recorded. The memberships are kept in the database, in Object Storage, as `lis-wake-be:…` tags on
the NodeBalancer and as `lis-nbm:…` tags (NodeBalancer, port, address, mode and weight) on the node's
own OS volume, so `restore` and `rebuild` bring them back, from the tags alone if need be -- also for
a node that was stopped at the time (without Object Storage, a node's own NodeBalancer label is then
replaced by its name).

### 8.25 Resize: move a node to another plan

A managed node has no local disk: its operating system and data are on Block Storage volumes, and
every start creates the instance from the node's record. Changing its plan (CPU, memory, shared or
dedicated) is therefore quick and doesn't touch the volumes.

```bash
python instance_manager.py resize --name app-1                       # current plan, every plan and its price
python instance_manager.py resize --name app-1 --type g6-dedicated-8 # move it
```

- **A stopped node** keeps its new plan in its record and uses it from its next start (scheduled,
  manual or a wake). Nothing happens now.
- **A running node** is stopped and started again on the new plan, which takes the usual few minutes
  of a stop and a start. The command asks you to type the name first (`--yes` skips the question).
  If Linode can't create the new plan (for example no capacity for it in the region), the node goes
  back to its old plan and is started again on it. An extension or manual-start timer it had is kept.
- Refused, with nothing changed, for a plan name Linode doesn't know or reports unavailable in the
  region, a plan too small for the node's volumes (8 volumes, or one per GB of memory above that), a
  node that is starting, stopping, unreachable, hibernated or being copied, and an LKE pool (a pool's
  size and type are changed in its own settings).
- The plan is kept in the node's record, its Object Storage copy and a `plan:` tag on its OS volume,
  so a recovery from tags alone brings the node back on its current plan.
- The dashboard's **Plan (resize)** card on the instance page lists the plans with their monthly price
  and the difference from the current one; you pay only for the hours the node runs.
- API: `GET /instances/{name}/resize` (plans) and `POST /instances/{name}/resize` with `{"type": ...}`
  (a running node also needs `"confirm_name"`); scope `instances:resize`.

## 9. Costs

While a node shows `stopped`:

- **No compute charge at all** — this is the whole point.
- **A Block Storage charge, billed per GB/month, for every volume attached to that node** (OS
  volume + any data volumes). This is a flat **$0.10/GB/month** in most regions — but note it
  **scales with the size of the disk being migrated**, not with the instance's plan cost. A
  small instance's ~25GB disk costs about $2.50/month to keep as a stopped volume; a large
  dedicated instance's disk costs proportionally more, since Path B's destination volume has to
  be at least as large as the source disk (see the worked example below).
- **A small idle charge for the reserved IP**, billed whether or not it's currently attached to
  anything (Linode's published rate is around $2/month — worth confirming against Linode's
  current pricing directly, since this specific figure wasn't independently re-verified for this
  guide the way the Block Storage rate was).

Both of these are ongoing while the node exists in any state, running or stopped — they're the
price of guaranteeing the node comes back identical.

**Worked example, to make this concrete for a large instance**: a **G7 Dedicated 32x16**
(640GB local disk, $346/month running) migrated via Path B needs a destination volume of about
645GB. At $0.10/GB/month, that's **~$64.50/month** while stopped (plus the same small reserved-IP
fee any node pays) — **roughly an 81% reduction** versus paying for it running around the clock,
not free, but a real, substantial saving. Smaller instances save a much higher *percentage*,
since their disks — and therefore their stopped-volume cost — are proportionally tiny next to
their running cost.

**One current limitation worth knowing**: Linode caps a single Block Storage volume at 16TB.
This tool doesn't support splitting a migration across multiple volumes, so Path B migration
isn't currently supported for a source disk that large (it fails cleanly with a clear error
before creating anything, rather than partway through) — not a concern for any instance type
available today, but worth knowing if you're planning around very large custom storage
configurations.

---

## 10. Realistic walkthroughs

### A development environment that runs only in office hours

Your team's development environment is three nodes — `app-1`, `app-2` and `db-1` — used
09:00–19:00 on weekdays in India. Put them in one group with that schedule:

```
linode-instance-scheduler group-create --name dev --timezone Asia/Kolkata
linode-instance-scheduler group-schedule-set --group-name dev --timezone Asia/Kolkata \
    --days mon,tue,wed,thu,fri --start-time 09:00 --stop-time 19:00
linode-instance-scheduler group-add --name app-1 --group-name dev
linode-instance-scheduler group-add --name app-2 --group-name dev
linode-instance-scheduler group-add --name db-1 --group-name dev
```

From then on they start every weekday morning and stop every evening, at the same addresses each
day. Compute is paid for 50 of the week's 168 hours — about a 70% saving. Someone working late
extends the group for a couple of hours from the dashboard; a public holiday added with
`holiday-add` keeps them stopped all day.

### An application that needs its database first

The application nodes in group `app` can't start until the database in group `db` is up and
accepting connections. Give the database a post-start check and make `app` depend on `db`:

```
linode-instance-scheduler hooks-set --group-name db --post-start 'pg_isready -t 5'
linode-instance-scheduler group-depends --group-name app --on db
```

Each morning the database starts first and the application nodes wait until its check passes;
each evening the application stops first, then the database.

### A rarely used tool that starts when someone needs it

An internal reporting server, `reports-1`, is used a few times a week at unpredictable times.
Instead of a schedule, keep it stopped and let people wake it:

```
linode-instance-scheduler wake-policy --name reports-1 --mode wake --idle-minutes 30
linode-instance-scheduler wake-link --name reports-1
```

Share the link. Opening it shows the node's state and a Start button; once nobody has used the
node for 30 minutes it stops again by itself.

### Standbys that wait for an incident or a traffic spike

You keep extra nodes — `standby-1`, `standby-2` — onboarded and `stopped` day to day. When you
need them (a failover, a busy day), start them:

```
linode-instance-scheduler start --name standby-1
linode-instance-scheduler start --name standby-2
```

A couple of minutes later they're up at their known, stable addresses with their data, and
rejoin whatever they belong to the way they always do. When you're done, `stop` them and they go
back to costing almost nothing.

---

## 11. Troubleshooting

**Start here:** `linode-instance-scheduler explain "<the error>"` (or "Explain this" on a dashboard
error) gives an error's cause and what to do, and `doctor` checks everything this tool relies on.
See §8.17.

**`onboard` refuses with "is not a reserved IP."** The node's public IP hasn't been reserved
yet. If you went through §6, this shouldn't happen (it's handled automatically in
`migrate-resume`). If you skipped §6 because the node was already on Block Storage, reserve the
IP yourself first (Cloud Manager, or the API), then re-run `onboard`.

**`start` fails partway through.** The tool retries transient failures (capacity issues,
momentary API errors) automatically with backoff before giving up. If it still fails, any
partially-created instance is automatically cleaned up rather than left behind as an orphaned,
billing resource — check `status --name <name>` and try again.

**A node looks "locked" but nothing is actually running.** See `clear-lock` in §8 — but confirm
independently in Cloud Manager first that nothing is genuinely in flight before using it.

**I changed something in Cloud Manager (renamed the node, added a firewall, re-tagged it) — will
that be lost?** No — every `stop` re-captures the node's current state fresh before deleting it,
specifically so an out-of-band change like this isn't silently reverted on the next `start`.

**A node was deleted manually in Cloud Manager (or the instance was otherwise removed outside
this tool) and the registry still thinks it's `running` against a now-`404` `linode_id`, and
your local `state/` directory is otherwise intact.** Just run `stop --name <name>` (or `start`) —
this tool already detects the confirmed-404 case automatically: it resets the record straight to
a clean `stopped` state, preserving every other already-known field (network config, authorized
keys, label, tags, plan, data volumes, reserved IP), with zero manual Cloud Manager steps needed.
Reach for `rebuild --force` below only if this doesn't apply — for this scenario the one-step
`stop`/`start` resolution above is the better outcome.

**Your local `state/` directory itself was lost (not just one node's registry entry) — a
disk failure, a machine rebuild, anything that wipes `state/instances.db` entirely — and you need
to reconstruct it from scratch.** Recover with:

```
linode-instance-scheduler rebuild --force
```

This rebuilds the registry entry from the disaster-recovery tags already on the surviving
OS/data volumes and reserved IP (see §8's `rebuild`). `--force` is required to overwrite a
stale existing entry, if any. Two outcomes:

- If a live instance is currently running against this node's volumes/IP (the normal case for
  a node that was simply `running` when local state was lost), the record comes back fully
  recovered and pointing at that instance — `stop`/`start` work normally right away.
- If nothing is currently running against those resources, `rebuild` takes the network config and
  SSH access from the node's own recovery tags (a simple single-interface node) or its Object
  Storage record, and the node comes back `stopped`, ready to `start`. Only when neither is enough
  is it flagged `needs_manual_recovery` — `start`/`stop` both refuse in this state. Get some
  instance running against those volumes/IP (Cloud Manager, or by re-running whatever
  process created the node originally), then run
  `onboard --name <name> --instance-id <live-instance-id> --force` to fully re-capture the
  remaining fields and clear `needs_manual_recovery`.

**`status`/`list` shows a node as `unreachable`.** Two distinct situations land here — both
resolve the same way, just run `start` again:
- `start` created a real instance (it's real and billing) but couldn't confirm it was actually
  reachable — a slow boot, a network blip, or (see the `SECURITY WARNING` case just below) a
  host-key mismatch.
- A node that was already `running` per this tool's own records was found powered off when
  `start` last checked it — e.g. someone used Cloud Manager's "Power Off" button directly,
  bypassing this tool entirely (that's a different action from this tool's own `stop`, which
  always deletes the instance rather than just powering it down).

Either way, `start` retries against that same existing instance — rebooting it first if it's not
already running/booting — rather than creating a second one. If the instance has since been
confirmed gone entirely (a 404 — e.g. you deleted it by hand in Cloud Manager while
troubleshooting), `start`'s retry (or `stop`) resets the node to `stopped` automatically, since a
genuinely-gone instance isn't ambiguous the way an unreachable one is; run `start` again after
that to recreate it.

**`start` refuses with a `SECURITY WARNING` about a changed SSH host key, right after upgrading
this tool.** This is the one-time migration case in §8 — the node was onboarded before this
tool started verifying host keys, so nothing was on record to compare against yet. Run
`reset-host-key --name <name> --yes` once for that node (and any other pre-existing node showing
the same thing), then `start` again. If you see this on a node that's been running fine on a
current version of this tool for a while, don't treat it as routine — confirm independently
(Cloud Manager's Lish console) that nothing's actually wrong before using `reset-host-key`.

---

## 12. Support

Questions or issues with this tool: contact **Sandip Gangdhar (sgangdhar@akamai.com)**.
