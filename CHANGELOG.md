# Changelog

All notable changes to this project. Versions follow [semantic versioning](https://semver.org/).
Upgrade by re-running `install.sh install` (see `DEPLOYMENT.md`), or by moving to the new
container image on Kubernetes (see `DEPLOYMENT-LKE.md`).

## v1.16.0

### Added
- `ask`: ask a question about the product or paste an error, and get the answer worked out on
  this host -- nothing is sent anywhere. A small model on the host's CPU chooses among the
  product's own reviewed answers (every feature: what it is, when to use it, how; day-2 tasks;
  every command) and its error catalog, and fills in this deployment's facts: its version and
  install type, a named node's effective schedule, holiday and extension, or a review of the
  deployment against the recommendations. Typos and other wordings are understood; when it isn't
  sure it says so and lists the closest topics. It learns on the host from "did this help?"
  feedback and from failures seen there. CLI `ask`, `ask-feedback`, `ask-learn`; API
  `POST /system/ask`, `POST /system/ask/feedback`, `POST /system/ask/learn`,
  `GET /system/ask/answers` (scope `system:ask`, in `read`); dashboard Ask page (with "Learn
  now") and an Ask card on Instances.
- `explain`: paste an error (or `--file`, or `--name` for a node's last failure) for its cause,
  read-only checks against this deployment and what to do; "Explain this" on every dashboard error
  banner. API `POST /system/explain`.
- `doctor --fix`: shows the repairs for what `doctor` found and applies them after you confirm
  (`--only`, `--name`, `--yes`); the dashboard's Consistency check card has "Review fixes". The
  active scheduler applies the safe ones itself after its daily check (re-upload an Object Storage
  record, rewrite a node's tags, clear a stale in-progress mark); `LIS_AUTOFIX=off` stops that.

## v1.15.0

**Upgrading from an earlier version:** this repository's history was restarted with this release,
so `git pull` in an existing clone won't work. Clone it again and run the installer from the new
clone; it upgrades the existing install in place and keeps its settings, database, keys and
backups:

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler && sudo ./install.sh install
```

### Changed
- Releases are now a compiled program for Linux (x86-64, glibc 2.28+: Ubuntu 22.04+, Debian 12+,
  RHEL/Rocky/Alma 9+): one archive per release, attached to the GitHub release with its SHA-256
  checksum, holding the program, its libraries and the built dashboard. Hosts no longer need Python
  or Node.js. `install.sh install` downloads the latest release (or `--version vX.Y.Z`, or
  `--archive FILE`), checks its checksum, installs it to `app-<version>/` with a `current` link,
  keeps the previous release for going back, and adds `linode-instance-scheduler` to run commands
  (`sudo linode-instance-scheduler <command>`). Upgrading an existing install is the same command:
  the old code is moved aside and `.env`, the database, keys and backups stay where they are.
- The container image is built from the release archive.
- The release copy in the bucket holds the installed program instead of source and dependency
  wheels.

### Added
- `token-check` (checks the API token) and `host-addresses` (this host's addresses, including a
  VPC 1:1 NAT address), used by the installer.
- Instances whose SSH server listens on a port other than 22: `onboard` and `migrate-start` take
  `--ssh-port`, `set-ssh-port` changes it later (checking a running instance on the new port
  first), and every connection the tool makes uses it. It is kept in the instance's recovery tags
  and Object Storage record, so `rebuild` restores it. The dashboard's Onboard page and migration
  wizard send the port from the SSH access section, and the instance page has an SSH port card.
- Dashboard "Test login": logs in once with the chosen credential (default key, private key or
  root's password) on the chosen port and says whether the port, the host key or the credential
  is the problem. Nothing is stored. API: `POST /linode/ssh-login-check`.
- Dashboard migration wizard: the copy step shows the single used-block copy command (it finds
  both disks itself and copies only the blocks in use, so a large, mostly empty disk takes minutes)
  and finishes only once its output shows `COPY_DONE`; the step-by-step whole-disk `dd` stays
  available. The start screen offers the two-phase copy (short downtime). `GET
  /instances/{name}/migrate-status` returns the command as `copy_command`, and `migrate-start`
  takes `two_phase`.
- Dashboard: migrate several instances in waves from the Onboard page's Account readiness report.
- Waves take an SSH port per instance: `migrate-wave --instance NAME=ID:PORT`, `ssh_port` on each
  instance in `POST /migrations/waves`, and a port box per instance in the dashboard.

- The same features in the CLI, the API and the dashboard:
  - Dashboard **Maintenance** page: rebuild from the account's tags, check and repair now,
    leftover migration volumes (list, delete, mark), credential rotation, recovery drill (region,
    keep the VM), the release copy, bucket versions (list and restore an older version), send
    alerts now, and forget a host key by address.
  - Instance page: start/stop options (skip hooks, respect the start order, the automatic-stop
    window, stop without reading the node, stop despite a tag-refresh failure), offboard without
    the pending period, "Reset trusted host key" next to a host-key warning, a shared-network check,
    and more history. Group "Run now" options (skip hooks, respect start order, members at a time);
    backup delete without the pending period; "show finished" pending deletions; readiness with
    managed instances; cancel a migration without booting the original; run the assisted copy
    without finishing.
  - API: `POST /instances/{name}/reset-host-key`, `POST /linode/ips/{address}/reset-host-key`,
    `POST /system/rebuild`, `GET /migrations/orphans` with `/cleanup` and `/mark`,
    `GET /instances/{name}/network-neighbours`, `GET /system/bucket-protection/versions` and
    `POST .../restore`, `POST /system/health/send`; `POST /system/heal` takes `names` and
    `leftover_min_age_minutes`, the recovery drill `region` and `keep_vm`, the catch-up
    `max_parallel`, the assisted copy `resume`. New token scopes `instances:reset-host-key` and
    `system:rebuild` (in `configure`).
  - CLI: `savings`, `activity`, `hook-history`, `reserve-ip`, `ssh-check [--login]`,
    `backup-estimate`.

### Changed
- The dashboard's SSH port field used to apply only to the reachability test; it is now the port
  onboarding and migration connect on.

## v1.14.3

### Fixed
- Upgrading with `install.sh install` while the services are running could stop with
  `chown: changing ownership of '.../state/instances.db-shm': No such file or directory`: the
  running scheduler creates and deletes SQLite's temporary files while the installer walks the
  directory. Those files are now skipped and a file that disappears mid-walk no longer aborts the
  install. (Re-running the installer was already safe.)

## v1.14.2

### Fixed
- `offboard` without the grace period (`--now`, or `LIS_DELETION_GRACE_DAYS=0`) refuses while any
  of the node's volumes is attached to an instance or its reserved IP is assigned to one -- the
  record can be behind reality (a node booted by hand from its volumes, or a database restored
  from an older snapshot), and releasing the address or stripping the volumes' recovery tags would
  hit a live system.
- A pre-migration backup that was rolled back while its deletion was pending is no longer deleted
  when the grace period ends; a backup instance that is running, or a cloned volume attached to
  another instance, is never deleted by `backup-delete`.
- `rollback` refuses to replace an original instance that still runs from its own disk with no
  migration in progress (a cancelled migration it has kept running since).
- The self-healing check no longer takes an instance that merely has the node's OS volume
  attached as a second disk for the node, and `stop` refuses to delete an instance that doesn't
  boot from the node's OS volume.
- Recovering a pending deletion removes its "pending" marks before the node is registered again,
  so a later `rebuild` can't turn it back into a pending deletion.
- `migrate-orphans --cleanup` never deletes a volume attached to an instance.
- After `rebuild` recreated a group, its Object Storage copy still said it had no schedule rules
  until the group was next changed; `rebuild` now writes every group's copy again at the end.

## v1.14.1

### Changed
- Registering an LKE node pool (`pool-register`, `POST /pools`, scope `pools:register`) now needs
  an `admin` token, like onboarding an instance: it hands the pool's nodes to the scheduler, which
  removes them at the stop time. The `configure` bundle no longer includes it.

### Fixed
- `deregister` of an LKE node pool now refuses when its Object Storage record can't be marked as
  deregistered, instead of going ahead; before, a later `rebuild` could bring the pool back under
  scheduling.
- `rebuild` no longer counts an unreadable Object Storage record of a node it already reported
  (ambiguous, incomplete, invalid or pending deletion) as a failed pool recovery.
- `migrate-abandon` no longer says the instance was left running when it no longer exists.

## v1.14.0

### Added
- LKE node pools can be scheduled like instances (`pool-register`, `POST /pools`, or the Onboard
  page): stopping removes the pool's nodes, or scales the pool to one node when it is the cluster's
  only pool or system backups (Object Storage) aren't configured; starting
  brings the pool back with the same size and settings (including an LKE Enterprise pool's
  firewall and update strategy). Schedules, groups, holidays, start order,
  manual starts and extensions all apply. New token scope `pools:register`.
- `onboard-readiness`: checks every instance on the account and lists which can be onboarded now,
  which need a migration first (with the volume size, its monthly cost and a rough copy time) and
  which are blocked and why. Read-only. Also on the Onboard page and at `GET /linode/readiness`
  (new token scope `instances:readiness`).
- `migrate-wave` (and `POST /migrations/waves`): migrates several instances off local disk and
  onboards them a few at a time, each on its own, with pre-migration backups and the copy run for
  you.
- `migrate-start --two-phase`: copies most of the data while the instance keeps running, so the
  Rescue Mode copy only brings across what changed since — minutes offline instead of the time it
  takes to copy the whole disk. Needs an ext4 root filesystem and rsync on the instance.
- The Rescue Mode copy now copies only the blocks the filesystem uses for ext2/3/4 and xfs disks
  (and checks the copy before reporting COPY_DONE), so a mostly empty disk is copied in a fraction
  of the time. Other disks are still copied whole with `dd`. This applies to the copy command
  `migrate-start` prints and to the assisted copy; the dashboard's step-by-step manual copy still
  pastes a whole-disk `dd`.

### Fixed
- `migrate-abandon` no longer cancels a migration that `migrate-resume` has already started
  finishing. Before, after a `migrate-resume` that failed partway (for example while reserving an
  address), `migrate-abandon` could delete the new volume even though the instance by then booted
  from it and its original disk was gone. It now refuses -- run `migrate-resume` again to finish --
  and never deletes the volume an instance boots from.
- A recreated instance, and a migrated one, now keeps the kernel it booted with. Before, the boot
  configuration this tool creates on every start (and when a migration finishes) got Linode's
  default kernel, so a node deployed with its distribution's own kernel through GRUB 2 switched to
  Linode's kernel -- which on RHEL-family systems also turns SELinux off. The kernel is now
  recorded at onboarding and on every stop and used on the next start. A node already running on
  Linode's kernel keeps it; to go back to the distribution's kernel, set its boot configuration's
  kernel to "GRUB 2" in Cloud Manager while it's running -- the next stop records it.
- Rolling back to a pre-migration backup now removes the backup tag (`lis-backup-of:`) from the
  restored instance and its volumes. Before, `rebuild` kept listing a rolled-back instance as an
  untracked backup. For an instance restored with an earlier version, remove that tag in Cloud
  Manager.

## v1.13.0

### Added
- `doctor`: a consistency check that the database, the tags on your Linode resources, the Object
  Storage copies and the trusted host keys all agree — everything recovering a node depends on.
  Read-only; each problem is printed with how to fix it. Exit 0 when consistent, 1 when problems
  are found, 2 when a check couldn't run; `--json` for scripts. Also on the dashboard's System
  page and at `GET /system/doctor` (new token scope `system:doctor`, included in `read`).
- The active scheduler runs the consistency check daily and alerts on any problem, like the other
  health conditions; the next clean run clears the alert.
- `doctor` also lists (as a notice) resources still carrying the pre-migration backup tag older
  releases wrote (`linode-scheduler-backup-of:`). It is still recognised until v2.0.
- `.env.example` documents `LINODE_SCHEDULER_BACKUP_DIR` (where `backup` writes local snapshots).

### Fixed
- An SSH key rotation could finish while a node that had left the registry (for example with
  `deregister`) still trusted only the old key on its disk; once that node came back, the tool
  could no longer log in to it. A node that leaves during a rotation now keeps the old key
  available while anything on the account is still tagged for it, and a node added to the
  registry during a rotation joins it.
- Every command failed to start on Python 3.10 (a Python 3.11-only name had slipped in).
- API tokens: a token limited to some groups can no longer act on other groups through a group's
  start order (`with_dependencies`), or move its node into a group outside its limits; a token
  that expires can no longer create one that outlives it; previewing or restoring an item from a
  snapshot also needs that item's own scopes (restoring hooks needs `hooks:write`).
- The console's log file records who ran which command and its exit code, no longer the output
  (which is shown only in the console itself).
- The reachability check connects to the address it validated, and accepts only ports 1-65535.
- A hook that prints a lot no longer holds all its output in memory; the last part is kept.

## v1.12.5

### Fixed
- The official container image now serves the web dashboard at `/ui/`. Earlier images included
  the dashboard but served only the REST API (`/ui/` answered 404). Installs from the repository
  with `install.sh` were not affected.

### Changed
- The Python code behind `instance_manager.py` is now split into several `lis_*.py` modules
  next to it. Commands, options, the REST API and stored data are unchanged; if you copy files
  by hand rather than with `install.sh` or the container image, copy the `lis_*.py` files too.

## v1.12.4

### Fixed
- The assisted migration copy (`migrate-copy`, **Run the copy for me**) no longer gives up after
  six hours, so multi-terabyte disks can be copied. It fails only if `dd` makes no progress for 45
  minutes. A dropped Lish connection or a restart of the scheduler host no longer reports the copy
  as failed: the tool reconnects and keeps watching, or reads the result the copy recorded if it
  finished meanwhile. Starting the copy again while one is running watches the running copy
  instead of starting a second one.

## v1.12.3

### Changed
- A public address mapped through VPC 1:1 NAT is now reserved the same way a public interface's
  address is: when `migrate-resume` finishes, at onboarding, before every stop, and after a
  rollback. The node keeps the same public address across stops, starts and rollbacks. Nodes
  onboarded with an earlier version have their NAT address reserved at their next stop. If
  reserving fails you get a warning; onboarding and stops still go ahead, and `migrate-resume`
  stops so you can run it again.

## v1.12.2

### Fixed
- Rolling back a pre-migration backup of an instance that gets its public address through VPC
  1:1 NAT now reserves that address before the original instance is removed, so it is kept. If an
  earlier attempt already lost it, the rollback finishes with a new 1:1 NAT address and says so,
  instead of failing.

## v1.12.1

### Fixed
- `migrate-start` and `onboard` no longer fail pre-flight with an SSH host-key mismatch when Linode
  gave the new instance a VPC address a stopped managed node is recorded with. The stopped node is
  moved to a free address in the same subnet first, with its trusted host key, as its own next
  start would do; a node that isn't stopped, or is busy, is left alone with a warning.

## v1.12.0

### Added
- Health alert `key-rotation`: an SSH key rotation that isn't finishing (a node that needs manual
  recovery and so never moves, a node that keeps failing to move, or a rotation open for 7 days)
  now raises an alert, since the old key stays trusted until it completes.
- `rotate-credentials ssh-key` lists nodes that won't move by themselves separately, with how to
  unblock them (recover with `onboard --force`, or offboard/deregister).
- Dashboard: "Restore one item from a snapshot" picks the instance or group from a searchable
  list, and still accepts a name that no longer exists (for example a deleted group).
- `SECURITY.md` and this changelog.

### Fixed
- The release copy in the backup bucket now installs offline on Python 3.10 to 3.14, including
  dependencies published only as source.
- `install.sh recover --env-from-backup`: the age identity file could not be written on hosts
  that protect files in `/tmp` (Ubuntu), and a separate bucket settings file stopped the
  recovery after `.env` had been restored.

## v1.11.1

### Added
- Health alert `node-failing`: a node whose last three or more start/stop attempts failed, with
  no success since. A node stuck as "unreachable" is now reported instead of silently waiting.

### Fixed
- The recovery drill failed right after creating its throwaway VM and could leave that VM
  running. Drill VMs are now always found and deleted by their tag, and their host keys removed.

## v1.11.0

### Added
- **Pending deletion**: offboarding a node or deleting a pre-migration backup marks the resources
  for deletion after a grace period (7 days by default, `LIS_DELETION_GRACE_DAYS`). Pending
  deletions are listed in the dashboard, can be recovered exactly as they were or deleted now, and
  survive a total database loss. `--now` deletes immediately.
- A standby scheduler (another host holds the scheduler lease) refuses changes; a host that never
  held the lease stays on standby while the bucket is unreachable.
- Clock check: an alert when the host's clock is more than 60 seconds off or not synchronized.
- Dependencies installed from a hash-locked `requirements.lock`.
- A release copy (code, built dashboard and dependency wheels) kept in the backup bucket, so a
  replacement host can install without GitHub or PyPI.
- Object Storage versioning with a 30-day rule for old versions, and `bucket-protection` to bring
  back an older version of a backup object.
- `restore-item`: restore one schedule, one node's hooks, or one group from a database snapshot.
- `rotate-credentials` for the Linode API token, the bucket key, the OAuth secret and the
  deployment SSH key (nodes move to the new key without losing access).
- `recovery-drill`: a throwaway VM installs the release copy, restores read-only, checks every
  node and is deleted. Runs weekly with `LIS_RECOVERY_DRILL_DAYS`.
- API token scopes for each new operation; the dashboard token page picks nodes and groups from
  searchable lists.

## v1.10.0

### Added
- Scheduler lease: only one scheduler acts at a time; another host stands by.
- Daily restore test of the latest backup, Shutdown Watchdog on the scheduler host, optional
  heartbeat URL for an uptime monitor, alerts for an expiring or rejected API token and a nearly
  full disk.
- A damaged database snapshot is never uploaded over a good one.
- Alert settings editable from the dashboard and `alerts-config`.

## v1.9.1

### Added
- An operator-only, age-encrypted copy of `.env` and the deployment SSH key in the bucket, and
  `install.sh recover --env-from-backup` to restore it on a new host.

## v1.9.0

### Added
- Recovery after losing the scheduler host: a database behind the account adopts the real
  instance instead of creating a second one; backups on every change; the deployment SSH key kept
  encrypted with its passphrase escrowed in your account; review and apply missed schedules after
  an outage; cancel an unfinished migration; webhook and email alerts.

## v1.8.21
- Migration console session logs can be deleted, and expire after the retention period.

## v1.8.20
### Fixed
- `install.sh --domain` no longer warns when the domain points at the host's VPC 1:1 NAT address.

## v1.8.19
### Fixed
- Pre-migration backups failed for node names longer than 23 characters, and their cleanup.

## v1.8.18
- Migration console sessions are viewable in the dashboard while and after a copy runs.

## v1.8.17
- Groups and nodes can choose to ignore account-wide holidays.

## v1.8.16
- Holidays (account-wide, per group, per node) skip scheduled starts.
- An instance's own extension takes precedence over its group's.
- Assisted migration copy over the Lish console, when a pre-migration backup is kept.

## v1.8.15
- Extend a whole group past today's stop; extending keeps the start order.

## v1.8.14
- Optional pre-migration backup and one-step rollback.
- "Keep running past today's stop" for a scheduled node.
- Backup settings from the dashboard and `backup-config`.

## v1.8.13
- The scheduler checks every 15 seconds and runs starts and stops on a worker pool, so start-order
  chains don't wait a full interval per step.

## v1.8.12
### Fixed
- VLAN-only nodes keep their SSH host keys across stop/start.

## v1.8.11
- VPC 1:1 NAT supported on the older configuration-profile network model; start checks the
  reserved IP is still available first.

## v1.8.10
- API tokens can hold single operations, not only the read/operate/configure/admin bundles.
- `status` prints pure JSON on stdout.

## v1.8.9
- Schedule, group and manual-only recovery tags are rewritten on every start and stop; the
  individual schedule is also kept in Object Storage.

## v1.8.8
- API token records are kept in Object Storage, so a restore never brings back a revoked token.

## v1.8.7
- Dashboard Activity, Logs and Console pages.

## v1.8.6
- VPC 1:1 NAT supported on Linode Interfaces.

## v1.8.5
- A start whose VPC address was taken while the node was stopped moves the node to a free address
  automatically; onboarding warns about unmanaged instances on the same network.

## v1.8.4
- `set-vpc-address` moves a stopped node to a new VPC address; onboarding refuses an address
  another managed node already has.

## v1.8.3
- Migration supports the Linode Interfaces model; the dashboard can restart a stale or
  interrupted migration.

## v1.8.2
### Fixed
- Migration keeps every network interface of the original instance.

## v1.8.1
- VPC-only instances migrate and are checked over their private address.
- `install.sh --domain` sets up HTTPS with Caddy.

## v1.8.0
- A group can depend on several groups, with the full start order recoverable.
- `install.sh` installs on a fresh host or recovers onto a replacement.

## v1.7.0
- Group start order (dependencies), manual-only nodes, scoped API tokens, `wait=true`, start or
  stop a whole group in order, CLI exit codes and `--json`.

## v1.6.2
- Every Linode OS image supported: per-OS network configuration and a safer rescue copy command.

## v1.6.1
- VPC-only networking fixed; overnight schedules.

## v1.6.0
- The scheduler handles large fleets in parallel and paces Linode API requests.

## v1.5.0
- Hooks recoverable from Object Storage and tags.

## v1.4.0
- Pre-stop and post-start hooks.
### Fixed
- Recreated instances came back without DNS servers.

## v1.3.5
### Fixed
- A quick second click on a different dashboard action was ignored.

## v1.3.4
- VPC-only and VLAN-only instances supported.

## v1.3.3
- Recovery tags kept on the OS volume, so instances without a public IP are covered.

## v1.3.0 – v1.3.2
- Kubernetes (LKE) deployment path with an official container image.

## v1.2.4 – v1.2.6
- Optional Object Storage backup alongside tag-based recovery, and the `backup` command.

## v1.0.0 – v1.2.3
- Initial releases: the delete/recreate engine, CLI, scheduling, groups, manual override, savings
  display, REST API with "Login with Linode", and the web dashboard.
