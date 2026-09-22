# Redis / TiDB Standby Automation — Setup & Usage Guide

This guide walks you through everything needed to use this automation, end to end: building a
node the normal way, pulling this tool from GitHub, configuring it, migrating an existing node
onto the storage layout the automation requires, and running it day to day.

It assumes no prior familiarity with this tool. If you follow it top to bottom on a real node,
you'll end with a working `start`/`stop` workflow for that node.

---

## 1. What this is, and why it exists

On AWS, a common pattern for Redis failover standbys and TiDB replica pools is: keep several
instances built and ready, but **stopped**, until you actually need one. Stopping an EC2
instance is free — you only pay for the storage. When you need a standby, you start it and
repoint DNS (or, for TiDB, bring a replica online), rather than debugging a live problem under
pressure.

**That exact pattern does not work on Linode as-is**, because of one billing difference:

> A *stopped* Linode instance bills at the same rate as a running one. Powering it off does not
> stop charges. The only thing that actually stops compute billing is fully **deleting** the
> instance.

So a literal "stop" isn't the right tool here. This automation instead **deletes the instance
when you don't need it, and recreates it the moment you do** — but it's built so that from your
side, it behaves exactly like stop/start:

- The **public IP never changes** across cycles (it's reserved to your account, not tied to the
  instance's lifecycle) — no DNS or allow-list updates needed.
- **Data is never lost** — it lives on a separate Block Storage volume, independent of the
  instance, and survives every delete/recreate cycle.
- The **SSH host key stays stable** — no "remote host identity changed" warnings when you
  reconnect after a recreate.
- **Label, tags, plan/size, firewall, and other instance settings** are all captured and
  reapplied automatically, so the recreated instance looks identical in Cloud Manager and in
  your cost reports — not like a brand-new resource.

The only real difference from AWS: while "stopped," you pay a small ongoing fee for the Block
Storage volume(s) and for the reserved IP sitting idle — both far cheaper than running compute
24/7, but not literally free. See [§9, Costs](#9-costs) for the numbers.

### Use cases this is built for

- **Redis failover standbys.** You keep one or more pre-configured Redis nodes ready but not
  running. When your primary misbehaves, you start a standby, point your application at it, and
  investigate the original at your leisure — rather than firefighting a live node under load.
- **TiDB replica pool.** You keep a pool of pre-configured replica nodes ready. When you need
  more read capacity, you bring one online; when load drops, you take it back down.

Both are the same underlying pattern: a named, pre-configured node that's brought up and torn
down on demand, always coming back identical. The commands in this guide are the same for
either.

---

## 2. Prerequisites

Before you start, make sure you have:

- **A Linode account** with permission to create instances, volumes, and reserved IPs.
- **A Linode Personal Access Token** — Cloud Manager → Profile (top right) → API Tokens →
  "Create a Personal Access Token." Scopes needed: **Linodes** (Read/Write), **Volumes**
  (Read/Write), **IPs** (Read/Write). Account (Read Only) is enough to satisfy the tool's own
  startup check.
- **Access to this GitHub repository** (you should already have this).
- **A dedicated SSH key pair** for this automation to use. Don't reuse a personal key you use for
  other things — generate one specifically for this tool, so its access is clearly scoped and
  easy to revoke independently later. Generate it at the exact path the tool defaults to, so you
  don't need to add a `--ssh-key` flag to every command in this guide (the default is
  `~/.ssh/linode_spike_key` — a name left over from this project's prototype phase, harmless to
  use as-is; every command below also accepts `--ssh-key <path>` if you'd rather point it
  somewhere else):
  ```
  ssh-keygen -t ed25519 -f ~/.ssh/linode_spike_key -C "linode-instance-scheduler"
  ```
  You'll add the **public** half (`~/.ssh/linode_spike_key.pub`) to every node you want this
  tool to manage.
- **Python 3.10 or newer** and **git** installed on the machine you'll run this from — this
  should be a **centralized, persistent server** (a dedicated admin box or bastion host), not
  someone's personal laptop. See §4 for why.

---

## 3. Building a node the normal way

This section is exactly what you'd do today, with no automation involved — building a Redis
standby or TiDB replica node from scratch. If you already have existing nodes running Redis or
TiDB, skip to [§4](#4-installing-and-configuring-the-automation) and come back to
[§6](#6-one-time-migration-onto-block-storage-path-b) for what's different about bringing an
*existing* node under this tool's management.

### 3.1 Create the Linode instance

In Cloud Manager (or via the API/CLI, if you prefer): create a Linode the normal way — pick a
region, a plan sized for your workload, and a Linux image (Ubuntu 24.04 LTS is what this
automation has been validated against; other recent distros should work the same way, but
haven't been tested here). Nothing special is required at this stage — this is an entirely
ordinary Linode, local disk and all.

Add the automation's SSH public key (from [§2](#2-prerequisites)) to the instance at creation
time (Cloud Manager's "SSH Keys" field), or add it afterward:

```
ssh-copy-id -i ~/.ssh/linode_spike_key.pub root@<instance-ip>
```

**This step matters**: the automation needs to be able to SSH into your node using this key —
both to inspect it during onboarding, and because whatever's in `authorized_keys` at onboarding
time is exactly what gets carried onto every recreated instance afterward. If this key isn't
present, onboarding will fail.

### 3.2 Install and configure Redis

```
apt update && apt install -y redis-server
```

Edit `/etc/redis/redis.conf` for your actual requirements — at minimum, review:

- `bind` — restrict this to the interfaces/IPs that should actually be able to reach it (don't
  leave it open to `0.0.0.0` unless you have a firewall doing that job instead).
- `requirepass` — set a real password; don't run Redis unauthenticated.
- **Persistence** — this is the setting that matters most for this automation. Decide between
  RDB snapshots (`save` directives) and/or AOF (`appendonly yes`), and make sure whichever you
  choose is actually **writing to the data volume you'll attach in the next step**, not to local
  disk. Local disk does not survive a delete/recreate cycle; the data volume does.

Restart Redis after any config change: `systemctl restart redis-server`.

### 3.3 (TiDB) Install and configure TiDB / TiKV / PD

TiDB's own install tooling (`tiup`) and cluster topology are outside the scope of this guide —
follow TiDB's own documentation for provisioning your cluster's components. The one thing that
matters for *this* automation is the same as for Redis: **make sure each node's actual data
directory is on the Block Storage volume from the next step, not local disk.** Everything else
about how you configure and operate TiDB is unchanged by this tool.

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

Point Redis's persistence files (or TiDB's data directory) at `/mnt/data`, and restart the
service so it's actually using it.

At the end of this section, you have a completely ordinary, real, running node — Redis or TiDB,
your normal config, a data volume with real data on it, reachable at whatever public IP Linode
assigned it. Nothing about it is special yet; this is exactly the starting point the automation
expects.

---

## 4. Installing and configuring the automation

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

Clone the repository — that's where the actual tool lives:

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler
```

Set up a Python virtual environment and install dependencies:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create your local config file (this file is git-ignored — it never gets committed, and you
should never paste your token into chat, a commit, or anywhere else that isn't this file):

```
cp .env.example .env
```

Open `.env` and fill in the token from [§2](#2-prerequisites):

```
LINODE_API_TOKEN=your-real-token-here
```

Verify the Python environment and dependencies are set up correctly:

```
python instance_manager.py list
```

This should print `No instances onboarded yet.` (or a list, if you already have some) — it
confirms your local setup is working, though it doesn't check the API token yet (`list` is a
purely local command, deliberately, so it works even before you've configured anything).

To verify the token itself, run:

```
python -c "import linode_engine as e; c = e.build_client(e.load_token()); e.auth_check(c); print('token OK')"
```

If this prints `token OK`, everything is configured correctly. If it errors, double-check the
token's scopes and that `.env` is in the repo root, next to `instance_manager.py`.

---

## 5. The commands, what each one is for

Every command below is run from the repository root, with the virtual environment active
(`source .venv/bin/activate` if you started a new terminal session).

| Command | What it does |
|---|---|
| `migrate-start` | **One-time, only for a node whose OS is still on local disk.** Starts moving it onto Block Storage — creates the destination volume and boots the node into Rescue Mode. Prints one manual command for you to run. |
| `migrate-resume` | Finishes what `migrate-start` began, after you've run that one manual command. Boots the node from its new Block Storage volume and reserves its IP. |
| `migrate-orphans` | Lists (or `--cleanup`s) destination volumes left behind by a `migrate-start --force` restart — see §6.4. You'll rarely need this. |
| `onboard` | Registers an already-running, already volume-based node with this tool by name. Pure capture — reads the node's current state, changes nothing on it. |
| `start` | Brings a stopped node back online — recreated identically at the same IP. |
| `stop` | Takes a running node offline — deletes the instance, keeps its data and IP. |
| `list` | Shows every node you've onboarded and its current status at a glance. |
| `status` | Shows the full captured record for one node. |
| `history` | Shows the audit trail (create/delete events) for one node — "why was my instance down at 9am?" Works even for a node you've since `offboard`ed. |
| `schedule-set` | Sets (or replaces) one node's automatic start/stop schedule. See §8.5. |
| `schedule-show` | Shows one node's current schedule, if any. |
| `schedule-clear` | Removes one node's schedule — it goes back to manual-only control. |
| `group-create` | Creates a new, empty schedule group — give it a schedule afterward with `group-schedule-set`. See §8.6. |
| `group-schedule-set` | Sets (or replaces) a group's automatic start/stop schedule — applies to every current member. See §8.6. |
| `group-show` | Shows a group's schedule and its current members. |
| `group-list` | Lists every schedule group you've created. |
| `group-delete` | Permanently deletes a schedule group. Refuses if it still has members. |
| `group-add` | Adds (or moves) a node into a schedule group. |
| `group-remove` | Removes a node from its group — asks what to do about its schedule if it doesn't have one of its own. See §8.6. |
| `poll` | Runs the scheduler — checks every node's individual AND group schedule and starts/stops it if due, and auto-reverts any expired manual override. Run it continuously (the normal way), or `--once` from cron. See §8.5/§8.6/§8.7. |
| `serve-api` | Runs the optional REST API server — the same capabilities as the CLI, over HTTP, with "Login with Linode" auth. See §8.8. |
| `extend` | Pushes an active manual-override auto-stop timer further out. See §8.7. |
| `clear-lock` | Admin escape hatch — forcibly clears a stuck in-progress operation. You should rarely need this. |
| `reset-host-key` | Admin escape hatch — re-establishes SSH trust for a node after a genuine, confirmed key change. You should rarely need this either; see [§8](#8-day-to-day-usage) and the one-time migration note below. |
| `rebuild` | Disaster recovery — reconstructs your local registry from tags on your own Linode account, in case the machine running this tool (and its local records) is ever lost. You should rarely need this either. |
| `backup` | On-demand, whole-system backup — re-syncs every node's Object Storage record and takes a full local/remote database snapshot. Meant to be run on a schedule (cron/systemd timer). See §8.10. |
| `offboard` | Permanently decommission a stopped node — releases its reserved IP, removes it from tracking, and optionally deletes its volumes. For when you're actually done with a node, not just pausing it. |
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

### 6.1 `migrate-start`

```
python instance_manager.py migrate-start --name redis-standby-1 --instance-id <your-instance-id>
```

- `--name` — a short name **you choose**, to refer to this node by from here on. Doesn't have to
  match its Cloud Manager label.
- `--instance-id` — the numeric Linode instance ID (visible in Cloud Manager, or in the
  instance's URL).

**What this actually does:**

1. Runs two hard pre-flight checks over SSH: your node's `cloud-init` version (needs to be
   23.3.1 or newer) and its metadata datasource (needs to be Akamai's). Both are required for a
   later step in this tool to work correctly; it refuses to proceed if either is missing. It also
   refuses outright if your instance somehow has more than one boot config — Linode's API has no
   "which one is active" field, so this tool won't guess which one to read the current disk from
   (the same refusal `onboard`/`rebuild` have for the same reason).
2. Warns (but doesn't block) if it finds hand-configured networking outside the OS's normal
   Network Helper — e.g. a custom netplan file. If you've done this deliberately, that's fine to
   proceed past, just be aware it'll be overwritten by this tool's own network handling on the
   next recreate.
3. Creates a new, appropriately-sized Block Storage volume to hold the migrated OS.
4. Boots your instance into Rescue Mode, with the original disk and the new volume attached at
   fixed device paths.
5. Prints a `dd` command using those fixed device paths, along with the source disk size and
   destination volume size — **do not run it verbatim without the check in §6.2 below.**

**Output looks like this** (matches the tool's actual printed output exactly — this used to be a
fabricated example showing a step the tool didn't really print; fixed alongside the tool itself,
see §6.2):

```
YOUR TURN -- this is the one manual step in the whole process:
  1. In Cloud Manager, open instance <id> and click "Launch LISH Console".
  2. Log in as root.
  3. Run `lsblk` FIRST and match devices by size -- do NOT trust /dev/sda/sdb below blindly:
       source (original disk) should be ~20480MB
       destination (new volume) should be ~25GB
     If lsblk shows a different device for either size, substitute the correct device names into
     the command below instead of running it as printed.
  4. Once confirmed, run:

       dd if=/dev/sda of=/dev/sdb bs=4M status=progress && sync

  5. Once it finishes cleanly (no I/O errors), run:
       instance_manager.py migrate-resume --name redis-standby-1
```

### 6.2 The one manual step

**Important — verify device letters before running `dd`.** Rescue Mode's device assignment
(which of `/dev/sda`–`/dev/sdh` ends up as your original disk vs. the new volume) is not
guaranteed to match the fixed device paths printed above — this has been observed live: the
destination volume landed at `/dev/sda` and the source disk at `/dev/sdg`, not the printed
`/dev/sda`/`/dev/sdb`. Running the printed command blindly in that situation would copy in the
wrong direction (destroying your original disk's data) or against an empty device. **Before
running `dd`, run `lsblk` in the Lish console and match devices by size**: the source device
should match your original disk's size (shown earlier in `migrate-start`'s output as
`local_disk_size_mb`), and the destination should match the newly-created volume's size
(`dest_volume_size_gb`). Only run the printed `dd` command once you've confirmed by size which
device is which — if the printed `/dev/sda`/`/dev/sdb` don't match what `lsblk` shows, substitute
the correct device names into the command yourself instead of running it as printed.

In Cloud Manager, open the instance and click **"Launch LISH Console."** Log in as `root`.

Once you've confirmed the device letters as described above, `dd` copies the entire original disk onto the new volume,
block by block — `bs=4M` is just a chunk size for reasonable throughput, `status=progress` shows
you it's actually moving. Wait for it to print a final "records in/out" summary with **no I/O
errors** before doing anything else. Then run `sync`, which flushes anything still sitting in a
write cache to the actual volume — skipping this can mean the copy isn't fully durable yet when
you move on to the next step.

For a typical node this takes a few minutes, roughly proportional to how much data is actually
on the disk.

### 6.3 `migrate-resume`

```
python instance_manager.py migrate-resume --name redis-standby-1
```

**What this actually does:**

1. Builds a real boot configuration pointing at the migrated volume, and reattaches any other
   Block Storage volumes (like your Redis/TiDB data volume) that were already on the node.
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
Manager from the moment it's created, precisely so it's discoverable by tag even if
`state/migrations.json` itself is lost mid-migration, not just once an attempt is abandoned or
completes — `migrate-orphans`' listing below merges in anything found this way even if the local
archive doesn't know about it.) The old ID is also recorded locally in `state/orphaned_migration_
attempts.json` (keyed by the node's name) once the *new* attempt's `migrate-resume` finally
succeeds — check on these anytime with:
```
python instance_manager.py migrate-orphans --name redis-standby-1
```
and delete them once you're sure they're no longer needed:
```
python instance_manager.py migrate-orphans --name redis-standby-1 --cleanup
```
(add `--volume-id <id>` to target just one, if there's more than one). Requires typing the name
to confirm, same as `offboard` (add `--yes` to skip the prompt, e.g. in a script). This file is
never cleaned up on its own otherwise — nothing scans it or reminds you it exists beyond this
command.

Like `rebuild` (above), `migrate-orphans` returns `0` only when nothing is wrong for the name(s)
checked, and non-zero if any real conflict or a scan failure is found — a conflict is still
printed as a `WARNING` either way, but if you're calling this from a script, cron job, or an
automated DR runbook, check the exit code rather than scraping the printed summary.

If you've lost `state/migrations.json` entirely and `migrate-orphans` shows a volume still
tagged "active" for a migration you know is actually dead (confirmed by hand in Cloud Manager),
mark it orphaned so it becomes eligible for `--cleanup`:
```
python instance_manager.py migrate-orphans --name redis-standby-1 --volume-id <id> --mark-orphaned
```
This only ever retags — it never deletes anything on its own, and refuses if the volume isn't
actually tagged active for that name. Same confirmation prompt (type the name, or pass `--yes`)
as `--cleanup` above.

**A newly reserved or reused IP address won't accept an SSH connection during `migrate-start`,
even though the node is genuinely reachable.** This can happen if Linode reissues an address that
this tool's own trust records (`state/known_hosts`) still remember from a previous, unrelated
node — e.g. one you `offboard`ed earlier. Since the node in question hasn't been onboarded yet,
`reset-host-key`'s usual `--name` form doesn't apply here; use `--ip` instead:
```
python instance_manager.py reset-host-key --ip <the-address> --yes
```
Only do this after independently confirming (Cloud Manager, Lish console) that the address
genuinely belongs to the node you expect.

---

## 7. Onboarding

```
python instance_manager.py onboard --name redis-standby-1 --instance-id <your-instance-id>
```

If you just did a migration in §6, use the same `--name` you used there. If your node was
*already* on Block Storage (you skipped §6), pick any name you like.

**What this actually does — pure capture, nothing is changed on your node:**

- Confirms three things and refuses cleanly if any fail:
  - The node is currently **running** (needed to inspect it over SSH).
  - Its OS is genuinely on a Block Storage volume, not local disk.
  - Its public IP is **reserved**. (If you did §6, this is already true. If not, and this
    refuses, reserve the IP yourself first via Cloud Manager or the API, then re-run.)
- Reads and records: its network configuration, every attached data volume (including yours from
  §3.4, with its actual mount point and filesystem), the real contents of
  `/root/.ssh/authorized_keys`, its label and tags, its plan/size, any attached firewall, and its
  maintenance-policy/watchdog settings.

You'll see a summary printed of everything it captured — worth a quick glance to confirm it
looks right (in particular, that `data_volumes` shows at least 1 entry if you expected data to be
there).

From this point on, the node is under this tool's management by the name you gave it.

**If the node has a VPC interface, you may need `--vpc-id`.** For the newer `linode` interface
model, the VPC's ID is already part of what's captured — nothing extra needed. For the older
`legacy_config` model, the API doesn't expose `vpc_id` directly on the interface, so this tool
first tries to auto-discover it by scanning your account's VPCs for the one containing the
node's subnet. That works as long as the VPC is visible to this tool's API token. If it can't
find it (a scoping issue, or the VPC belongs to a different account/project), `onboard` refuses
with a clear error and you supply it explicitly:

```
python instance_manager.py onboard --name redis-standby-1 --instance-id <id> --vpc-id <vpc-id>
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
that lets you push through this one. That's deliberate: this tool tried, in an earlier version, to
support a confirmed "take over ownership" path, and found it couldn't actually make that safe —
suppressing the refusal never removed the *other* name's tag, so the "successful" result was a
resource claimed by two names at once, which is exactly what `rebuild`'s disaster-recovery scan is
designed to detect and refuse to guess about (for *either* name, not just the one that got
"transferred"). If you genuinely want to repoint a name at infrastructure another name currently
owns, offboard the other name first (`offboard --name <other-name>`, see §8) — that cleanly
releases its tags — then re-run this onboard. `--yes` still skips the identity-diff confirmation
above; it does not, and cannot, skip this refusal. If a `--force` re-onboard moves this name to a
**different reserved IP** than it had before (confirmed via either prompt), this tool's own
cached SSH trust for the *old* address is also cleared automatically — the same cleanup
`offboard` already does when it releases an IP — so a future reissue of that old address to an
unrelated node won't need a manual `reset-host-key --ip` first.

---

## 8. Day-to-day usage

> **One-time migration note — read this if a node in your fleet was onboarded with an older
> version of this tool, before SSH host-key verification existed.** `start`/`stop`/`rebuild`
> verify a node's SSH host key against a record this tool keeps itself (`state/known_hosts`) —
> strictly, by default, since a steady-state recreate is guaranteed to present the *same* key
> every time (see `start` below). That record has to exist first. A node with nothing recorded
> yet will fail its very next `start` with a `SECURITY WARNING` about a key mismatch — not
> because anything is actually wrong, but because there was nothing to compare against yet.
> **Fix, once per node with nothing recorded, before your next `start`/`stop`:**
> ```
> python instance_manager.py reset-host-key --name redis-standby-1 --yes
> ```
> This connects once, trusts whatever key the node currently presents, and records it. Do this
> for every node in this situation — after that, every future `start`/`stop` verifies against it
> automatically and you'll never need to touch this again unless you genuinely rotate a host's
> key on purpose (see `reset-host-key` below).

### `stop` — take a node offline

```
python instance_manager.py stop --name redis-standby-1
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
python instance_manager.py stop --name redis-standby-1 --force
```

**If the node itself is SSH-unreachable** (a guest firewall rule, a crashed `sshd`, a wedged
network stack), the re-capture step above will retry for a while and then `stop` will refuse,
with nothing deleted — reachability can't be assumed away when re-capturing data volumes needs
a live SSH session. If you've independently confirmed (Cloud Manager, Lish console) that the
node is genuinely unreachable and you just need to stop billing, add `--skip-precapture`:

```
python instance_manager.py stop --name redis-standby-1 --skip-precapture
```

This uses the last-known data volume list from the registry instead of a fresh SSH capture. The
trade-off: a data volume attached out of band since the last `start`/`stop` cycle would be
silently missed from that list. Only use it when you've confirmed the unreachability yourself —
see [Troubleshooting](#11-troubleshooting) for the full recovery sequence if the instance ends up
deleted out of band (e.g. via Cloud Manager) instead.

### `start` — bring it back online

```
python instance_manager.py start --name redis-standby-1
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
python instance_manager.py list
```

```
redis-standby-1: running  ip=172.x.x.x  region=in-bom-2  linode_id=123456
tidb-replica-1:  stopped  ip=172.x.x.x  region=in-bom-2  linode_id=None
```

(`in-bom-2` here is just an example, not a required region — your instances' regions are
captured from wherever they actually live. Any Block-Storage-capable core region is supported;
distributed compute regions aren't, since this tool requires the OS to live on a Block Storage
volume.)

### `status` — full detail on one node

```
python instance_manager.py status --name redis-standby-1
```

Prints the complete captured record for that node — useful for confirming exactly what will be
reproduced on the next `start`, or for debugging.

### `history` — "why was my instance down at 9am?"

```
python instance_manager.py history --name redis-standby-1
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
python instance_manager.py clear-lock --name redis-standby-1
```

You'll get a warning first and a `[y/N]` prompt — **only confirm after independently checking in
Cloud Manager that nothing is actually still in flight** for that node. Add `--yes` to skip the
prompt (e.g. in a script), but only once you're scripting around a case you've already verified is
safe. (If another `start`/`stop`/`rebuild`
process for this same node is genuinely still running right now, `clear-lock` refuses outright
with a clear error instead of racing it — this only ever matters for a lock left behind by
something that actually crashed.)

### `reset-host-key` — admin escape hatch for a genuine SSH key change

Every `start`/`stop` verifies a node's SSH host key against what this tool already has on record
for it (see the one-time migration note above) — and refuses if it doesn't match, rather than
silently trusting whatever's presented. That's deliberate: it's exactly the check that would
catch a real problem (wrong disk, a misconfigured DNS/routing issue, or worse). This command is
the *only* way to move past a genuine mismatch:

```
python instance_manager.py reset-host-key --name redis-standby-1 --yes
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
python instance_manager.py reset-host-key --ip 172.x.x.x --yes
```
Both forms behave the same otherwise: confirm by typing the name/address, then this tool tries a
fresh connection before actually forgetting the old key — if that connection fails, the old
trust is restored automatically rather than left blank, so a failed attempt never leaves the node
in a *worse* state than before you ran the command.

### `rebuild` — recovering from a lost local machine

Your local registry (the file this tool keeps track of your nodes in) lives only on whatever
machine you run it from. If that machine is ever lost entirely — hard drive failure, an
accidentally wiped laptop, whatever — this command gets you back.

**How it works**: every time you `onboard` or `stop` a node, this tool tags that node's OS
volume, data volume(s), and reserved IP directly in your Linode account with the mapping
("these resources belong to `redis-standby-1`"). That's the one piece of information that isn't
recoverable from Linode's API on its own — everything else about a node (its network config,
tags, size, etc.) can always be re-read from the live instance. So instead of backing up the
whole local file somewhere, the one thing that actually matters is written directly onto your
own Linode resources.

```
python instance_manager.py rebuild
```

This scans your account for those tags and rebuilds your local registry from them — no backup
file needed anywhere. Two outcomes, depending on whether each node happened to be running or
stopped at the moment you lost your local machine:

- **Node was running** — full recovery. Everything is re-read live, exactly like a fresh
  `onboard` — including the same refusal `onboard` has if the instance somehow has more than one
  boot config (Linode's API has no "which one is active" field, so this tool won't guess): that
  one name is skipped and reported, not silently recovered against the wrong config.
- **Node was stopped** — partial recovery. The tool can tell you which volumes and which IP are
  yours (so nothing is left as an orphaned, unlabeled resource you'd have to hunt down
  manually), but the finer detail (network config, SSH access, label/tags) was only ever knowable
  while that instance actually existed, and is genuinely gone — this tool won't guess at it. A
  node recovered this way is marked `needs_manual_recovery`; boot it once manually via Cloud
  Manager from the recovered `os_volume_id`, then run `onboard` again as normal to fully restore
  management.

If the tags for a name turn out ambiguous or incomplete for any reason (e.g. a stale tag left on
an old resource, or resources for one name somehow spanning more than one region — neither
should happen in normal use), that node is skipped and reported rather than guessed at; the
warning tells you exactly what to check in Cloud Manager before re-running.

**If the node had a schedule set (§8.5), or belonged to a group (§8.6), `rebuild` recovers those
too** — not just the node itself. Both are mirrored onto the node's reserved IP as tags the same
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
python instance_manager.py rebuild --vpc-id <vpc-id>
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
python instance_manager.py offboard --name redis-standby-1
```

**Requires the node to already be stopped** — `stop --name redis-standby-1` first if it's still
running. Offboarding refuses cleanly on a running node rather than doing something surprising
with a live instance.

By default, this:
- **Releases the reserved IP** back to Linode's pool, permanently. If you ever re-onboard a
  similar node later, it gets a brand-new address — this exact one is gone for good.
- **Leaves the OS and data volumes intact** — nothing about your actual data is touched — but
  removes this tool's internal tracking tags from them, so a future `rebuild` can never mistake
  them for a node that still needs recovering.
- **Removes the node from your local registry.**

You'll be asked to type the node's name to confirm before anything happens — this is a
one-way action, not something a stray `y` at a prompt should be able to trigger by accident.

**To also permanently delete the volumes**, not just release the IP:

```
python instance_manager.py offboard --name redis-standby-1 --delete-volumes
```

This destroys the actual data on those volumes — irreversibly. Only use it once you're certain
you'll never need that data again. The default (no `--delete-volumes`) is deliberately the
safer choice: you can always delete volumes manually later via Cloud Manager once you're sure,
but you can't undo a deletion.

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
python instance_manager.py deregister --name test-node-1 --yes
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
python instance_manager.py schedule-set --name redis-standby-1 --timezone Asia/Kolkata \
  --days mon,tue,wed,thu,fri --start-time 09:00 --stop-time 18:00
```

`--timezone` is any IANA name (`Asia/Kolkata`, `US/Pacific`, `UTC`, ...) — the schedule's own
day/time rules are resolved in that timezone, DST included, not UTC. `--days` is comma-separated
3-letter days (`mon`..`sun`). **Overnight schedules aren't supported yet** — `--start-time` must
be earlier than `--stop-time` on the same day; a schedule that runs past midnight will be
rejected with a clear error, not silently mishandled.

Need more than one rule (e.g. different hours on weekends)? Pass a full JSON array instead:

```
python instance_manager.py schedule-set --name redis-standby-1 --timezone Asia/Kolkata \
  --rules-json '[{"days_of_week":["mon","tue","wed","thu","fri"],"start_time":"09:00","stop_time":"18:00"},
                 {"days_of_week":["sat","sun"],"start_time":"10:00","stop_time":"14:00"}]'
```

**View or remove it:**

```
python instance_manager.py schedule-show --name redis-standby-1
python instance_manager.py schedule-clear --name redis-standby-1
```

`schedule-clear` just turns off automation for that node — it goes back to pure manual `start`/
`stop` control, nothing about the node itself changes.

**Run the scheduler** — nothing above actually starts/stops anything by itself until this runs:

```
python instance_manager.py poll
```

Runs forever (every 5 minutes by default — `--interval-seconds` to change it), checking every
onboarded node's schedule and firing `start`/`stop` if due. Stop it with Ctrl-C. In production,
run it under a supervisor (e.g. a systemd unit with `Restart=always`) so it comes back up on its
own after a crash or reboot — the same way you'd run any other long-lived process. Prefer cron
over a supervisor? `poll --once` runs exactly one check and exits — schedule that on whatever
interval you'd otherwise poll on.

Every schedule edit takes effect on the *next* tick automatically — there's no separate "reload"
step, and nothing to restart.

**Known limitations, for now:**
- Overnight schedules (crossing midnight) aren't supported.
- If `poll` is down/delayed longer than the match window (default 5 minutes), that day's
  transition is genuinely missed, not caught up later — acceptable for a cost-scheduling tool (a
  late start/stop costs at most a little extra compute), by deliberate design choice.

### 8.6 Groups — one schedule for several nodes at once

Have a set of nodes that should all follow the same schedule (e.g. a whole "Dev Environment")?
Create a group instead of setting the same schedule on each node individually — one edit updates
all of them.

**Create a group and give it a schedule:**

```
python instance_manager.py group-create --name "Dev Environment" --timezone Asia/Kolkata
python instance_manager.py group-schedule-set --group-name "Dev Environment" \
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
python instance_manager.py group-add --name redis-standby-1 --group-name "Dev Environment"
python instance_manager.py group-add --name redis-standby-2 --group-name "Dev Environment"
```

**A node's own individual schedule (§8.5) always wins over its group's**, if it has one — the
group only governs a node that has no individual schedule of its own. This lets you put a node
in a group for the common case, then give it its own schedule later if it ever needs different
hours, without having to remove it from the group first.

**View or manage the group:**

```
python instance_manager.py group-show --group-name "Dev Environment"    # schedule + members
python instance_manager.py group-list                                    # every group you have
```

**Remove a node from its group:**

```
python instance_manager.py group-remove --name redis-standby-1
```

If that node has no individual schedule of its own, this asks what to do — copy the group's
current rules into a new individual schedule for it (so its hours don't change), or leave it
manual-only. Skip the prompt with `--copy-schedule` or `--keep-manual`. If it already has its own
individual schedule, nothing to ask — it was already governed by that, not the group, and removal
just makes that explicit.

**Delete a group entirely:**

```
python instance_manager.py group-delete --group-name "Dev Environment"
```

Refuses if the group still has members — `group-remove` each one first. This is deliberate: it
guarantees a group deletion can never silently leave a node with no schedule as a side effect.

Run `poll` the same way as for individual schedules (§8.5) — one poller enforces both individual
and group schedules together, and a group also survives a total local database loss via
`rebuild` (§8), the same tag-based disaster-recovery guarantee everything else in this tool has.

### 8.7 Manual override — starting a node outside its own scheduled hours

If a node has a schedule (individual, §8.5, or inherited from a group, §8.6) and you `start` it
manually outside its own scheduled "on" hours, this tool automatically arms a countdown so it
doesn't stay running forever by accident:

```
python instance_manager.py start --name redis-standby-1
```

```
'redis-standby-1' is up at 203.0.113.10.
  manually started outside its scheduled hours -- auto-stops at 2026-08-25 20:00 UTC unless
  extended (`extend --name redis-standby-1`).
```

The default window is 2 hours from when you started it. `status` and `list` both show the same
countdown for as long as it's active:

```
python instance_manager.py status --name redis-standby-1
python instance_manager.py list
```

**Need more time?** Extend it explicitly — nothing extends itself automatically, you always have
to ask:

```
python instance_manager.py extend --name redis-standby-1
python instance_manager.py extend --name redis-standby-1 --hours 4    # a different window, just this once
```

Each `extend` resets the countdown to `--hours` (or the 2h default) from **now**, not stacked on
top of the old one. `poll` (§8.5) is what actually enforces the auto-stop — as long as it's
running (continuously, or on a cron via `--once`), an expired override gets stopped
automatically on the next tick, the same way a normal schedule does.

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
python instance_manager.py start --name redis-standby-1 --override-window-hours 6
```

### 8.8 The REST API (optional) — for a dashboard, or your own scripts

Everything above works purely through the CLI. If you want to build a web dashboard on top, or
call this tool from your own automation over HTTP instead of shelling out to the CLI, run the
API server instead:

```
python instance_manager.py serve-api --host 127.0.0.1 --port 8000
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

### 8.9 The web dashboard (optional) — a browser UI on top of the REST API

If you'd rather click around than script against the REST API directly, build the dashboard once
and it's served automatically by the same `serve-api` process:

```
cd web
npm install
npm run build
```

That produces `web/dist/`; the next time you run `serve-api`, visiting `https://<your-host>/` (or
`/ui/`) in a browser shows the dashboard instead of the plain API-docs pointer. Nothing else to
configure — it talks to the same API server it's served from, using the same OAuth setup as
§8.8.

**What it covers**: log in with the same "Login with Linode" flow §8.8 describes (a "Log in with
Linode" button, instead of visiting `/login` by hand); an Onboard page that picks an existing
Linode instance from your account and brings it under management (including a one-time SSH
password/key field for a node that doesn't yet trust this deployment's own key — it's used only
for that one attempt and never stored, see §3 above) — and, if that instance still
needs Path B migration (§6) first, a guided, three-step wizard for it, deliberately never printing
a guessed `dd` command up front the way the CLI's own §6.2 output does: (1) you run `lsblk` in
Lish and paste the output back — the page identifies which device is your original disk and which
is the new, empty volume purely by matching sizes against the real source-disk and
destination-volume sizes it already knows from the Linode API (entirely in your own browser — no
external service, nothing sent anywhere for this check), and shows you its best guess to confirm
or correct via two dropdowns before anything is built; (2) only once you've confirmed which device
is which does it build the actual `dd` command, with a working Copy button; (3) after you run it,
you paste `dd`'s own summary output back, and the page checks it for a clean completion (matching
"records in"/"records out" counts, no I/O error text) before letting you continue — a command that
doesn't look like it finished cleanly blocks the next step outright, with the reason shown. This
is an early, best-effort check on top of the real safety net, not a replacement for it — the
actual proof that the migration worked is still the root-device identity check performed over SSH
once you click "finish migration" (§6.2's own device-mismatch warning still applies if you're
using the CLI directly, which has no equivalent wizard: verify with `lsblk` before running `dd`,
don't rely on the printed device letters alone). A list of every onboarded
instance with its current status; a per-instance detail page for start/stop/extend, viewing and
editing its individual schedule, its scheduled-vs-actual savings percentages, its recent event
history, and two distinct one-way actions in its own separate cards — **Offboard** (permanently
decommissions the node on Linode too — releases the reserved IP, optionally deletes volumes, see
the `offboard` CLI section above) and **Remove from tracking** (the `deregister` CLI command's
dashboard equivalent — removes local tracking only, never touches the node itself, for a record
that's simply wrong rather than a node you're actually done with); a group list and per-group
detail page for the same schedule/savings view plus membership management. It's a thin client
over the REST API in §8.8 — every action it takes is one of that API's own endpoints, nothing the
dashboard can do that the CLI/API couldn't already do directly.

### 8.10 `backup` — scheduling your own whole-system backups

Every stop/onboard already backs that one node up automatically (see the disaster recovery
section of the Definitive Guide) — `backup` is a separate, explicit command for taking a backup
of *everything at once*, on your own schedule, rather than waiting for individual nodes to be
touched:

```
python instance_manager.py backup --backup-dir /var/backups/instance-scheduler
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
0 3 * * * root cd /opt/instance-scheduler && .venv/bin/python instance_manager.py backup --backup-dir /var/backups/instance-scheduler >> /var/log/instance-scheduler-backup.log 2>&1
```

Exits non-zero if anything didn't complete (a per-node re-sync failure, either snapshot
destination failing) — check the exit code if you're wiring this into your own monitoring rather
than just reading the log.

### 8.11 High availability — why this isn't an always-on active-active or active-passive setup

Run exactly **one** `poll` process at a time, on one machine — never two running simultaneously,
and never a hot standby waiting in the wings. That's not a corner cut; it's the right fit for
what `poll` actually does.

`poll` doesn't serve live requests — it checks every schedule on a short, fixed interval with a
tolerant matching window (typically a few minutes either side), and fires the due action. If
that process is briefly down (a crash, a host reboot, a deploy), the worst case is a due action
firing a few seconds late once your process supervisor brings it back up — nothing waits on it
in real time, so nothing downstream notices. Manual `start`/`stop` keeps working the entire time
regardless, since it never depends on `poll` being up.

An **active-active** setup (two schedulers running at once) would trade that harmless delay for
a real correctness risk instead: two processes with no genuine coordination between them could
race to act on the same node at the same moment, or have one fire a start while the other is
mid-firing a stop for it — a new failure mode, introduced to solve a problem (a few seconds of
downtime) a plain restart already solves for free. An **active-passive** setup avoids the double-
fire risk, but only by adding its own always-on standby plus real failure-detection machinery
(health checks, a way for the standby to agree it should take over, a shared view of state) —
infrastructure that has to be running and paid for continuously, to protect against an outage a
single supervised process already recovers from in seconds.

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

### Redis failover

You maintain two standby nodes, onboarded as `redis-standby-1` and `redis-standby-2`, both
`stopped` day to day.

Your primary Redis starts misbehaving. Rather than debugging it live:

```
python instance_manager.py start --name redis-standby-1
```

A couple of minutes later, it's up at its known, stable IP. Repoint your application at it (DNS,
config, however you normally do failover). Investigate the original primary at your leisure —
no pressure, no live production system to poke at while it's still serving traffic.

Once you've resolved the original issue and are ready to switch back:

```
python instance_manager.py stop --name redis-standby-1
```

`redis-standby-1` goes back to costing almost nothing until you need it again.

### TiDB replica pool scaling

You keep three replica nodes onboarded — `tidb-replica-1`, `-2`, `-3` — normally all `stopped`.
A traffic spike means you need more read capacity:

```
python instance_manager.py start --name tidb-replica-1
python instance_manager.py start --name tidb-replica-2
```

Both come up at their stable, known IPs and rejoin your cluster the way any TiDB replica would.
When the spike passes:

```
python instance_manager.py stop --name tidb-replica-1
python instance_manager.py stop --name tidb-replica-2
```

---

## 11. Troubleshooting

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
Reach for `rebuild --force` below only if this doesn't apply — it's a strictly worse outcome
when nothing new happens to be running against the old resources yet (see the "partial record"
case just below) than the one-step `stop`/`start` resolution above.

**Your local `state/` directory itself was lost (not just one node's registry entry) — a
disk failure, a machine rebuild, anything that wipes `instances.json` entirely — and you need
to reconstruct it from scratch.** Recover with:

```
python instance_manager.py rebuild --force
```

This rebuilds the registry entry from the disaster-recovery tags already on the surviving
OS/data volumes and reserved IP (see §8's `rebuild`). `--force` is required to overwrite a
stale existing entry, if any. Two outcomes:

- If a live instance is currently running against this node's volumes/IP (the normal case for
  a node that was simply `running` when local state was lost), the record comes back fully
  recovered and pointing at that instance — `stop`/`start` work normally right away.
- If nothing is currently running against those resources, `rebuild` can only recover a partial
  record (network config and authorized keys aren't knowable from tags alone with nothing live to
  read them from) and flags it `needs_manual_recovery` — `start`/`stop` both refuse in this state.
  Get some instance running against those volumes/IP (Cloud Manager, or by re-running whatever
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
