# Changelog

All notable changes to this project. Versions follow [semantic versioning](https://semver.org/).
Upgrade by re-running `install.sh install` (see `DEPLOYMENT.md`), or by moving to the new
container image on Kubernetes (see `DEPLOYMENT-LKE.md`).

## v1.24.0

### Added
- **Snapshot-first migrations.** `--method snapshot`: a Linode Backups snapshot is taken while the instance runs, then a short one when it is powered off, before migrating in place; restored automatically if the check after migrating fails. Backups is left as the original had it, and the summary says what was done.
- **Check after migrating.** Every migration checks that every filesystem in the migrated system's `/etc/fstab` is mounted; a failure rolls back automatically (no-touch, clone, snapshot) or is reported (in place).
- **Re-hibernate.** Hibernating a node again reuses the copy its unhibernate left when the node hasn't run since, and otherwise uploads only the chunks that changed. `hibernate --fresh` makes a full copy.
- **No-touch onboarding (the new default).** Migrating an instance off its local disk no longer has to change it: its disk is copied to a volume, it is powered off, and a new instance with the same public IP, VPC/VLAN addresses, plan, kernel, label and data volumes boots from the copy. The original stays powered off as the fallback (listed on the Backups page with its monthly cost) until you delete it; Roll back puts it back exactly as it was. If the switch fails, the original gets its addresses back and is booted again automatically. `migrate-start --method no-touch|clone|in-place`, the same choice on the migration screen and for waves.

### Fixed
- After a total loss of the local database during a hibernation's grace period, `rebuild` recovered the node from its kept volumes as if it had been unhibernated, leaving its Object Storage copy behind. It is now restored as hibernated.
- A boot config's root device is kept through a migration, every stop and every start (it was always `/dev/sda`), so an instance booting Linode's kernel from a partition comes back the same way.
- Checking that a migrated system runs from its copy now works when the root filesystem is on a partition of the copied volume (for example a GPT disk).
- Stopping an instance right after an in-place migration is no longer refused while Linode is still deleting the old local disk.

## v1.23.0

### Added
- **Hibernate: Pause and Cancel while a copy runs.** The instance page has Pause and Cancel buttons during a hibernate or unhibernate (`hibernate --pause`, `--cancel`). Pause lets the chunks in progress finish, deletes the helper VM and keeps every checked chunk; Resume carries on from there with a new helper. Cancel gives the copy up: a hibernate leaves the node with its volumes as they were, an unhibernate deletes the volumes it created.
- **Hibernate leftovers on Pending deletion.** A hibernated node's kept volumes and the Object Storage copy an unhibernate leaves are listed on the Pending deletion page with their deletion date and a Delete now button. The Hibernate card shows where the copy is (bucket, folder, manifest).
- **Resize: move a node to another plan.** `resize --name X --type <plan>`, or the Plan (resize) card on the instance page, which lists the plans with their prices. A stopped node uses the new plan from its next start; a running node is stopped and started again on it (and goes back to its old plan if the new one can't be created). New API token scope `instances:resize`.
- **Hibernate: optional fast check.** `hibernate --fast-check` (or the "Fast check" box) checks each chunk on the helper and by its upload checksum instead of downloading it again: about half the transfer, at most a little faster (the copy is limited by the upload and the helper's CPU), slightly weaker. The full check stays the default; the estimate shows both times and transfers.

### Fixed
- A node with a hibernate copy in progress (running, paused or stopped) can no longer be started or offboarded until the copy is finished or cancelled.

## v1.22.1

### Added
- **Hibernate: what storage and transfer cost.** `hibernate --estimate` and the dashboard's Hibernate card compare, at your region's Linode prices, the monthly cost of the data on Block Storage and on Object Storage, and the network transfer hibernating and unhibernating can use (charged only past your account's transfer pool). The helper VM's cost is now labelled as a one-time cost.

## v1.22.0

### Added
- **Hibernate: choose the helper VM, see the time it takes.** `hibernate` and `unhibernate` take `--helper-type` (and the dashboard a list): the helper runs two copy workers per vCPU, so a bigger type copies several chunks at once and finishes sooner. `--estimate` and the dashboard show, for each type, the expected time for your volumes and what the helper costs.
- **Hibernate progress.** While a copy runs, the instance page shows its progress (chunks, GB, speed, time left), `hibernate --status` prints it, and the Activity log notes every 10%.
- **Resume and abandon.** Every checked chunk is recorded. A chunk that fails is handed out again with fresh links; a copy interrupted by a restart is carried on by the scheduler; a copy that stops can be resumed from where it was (**Resume**, or the same command again) or given up (**Abandon**, `--abandon`).

- **Ask** answers questions about very large hibernations, how hibernated data is compressed, and installing a specific version with `install.sh install --version`.

### Changed
- Hibernate copies faster: chunks are compressed with zstd instead of gzip, and a chunk that doesn't get smaller (already-compressed data) is stored as it is. On the smallest helper, data that doesn't compress copies about three times faster than before. Copies made by earlier versions are still read back as before.

### Fixed
- A hibernate or unhibernate of a large volume could fail after 12 hours, because every upload/download link was created at the start. Links now last two hours and are handed out a few at a time as the copy goes, so a copy can take as long as it needs.
- `install.sh install --version vX.Y.Z` run from an installed copy (`/opt/linode-instance-scheduler/current/install.sh`) reinstalled the version it belongs to and ignored `--version`. It now downloads the version asked for.

## v1.21.3

### Changed
- Choosing **Wake on demand** (stopped until someone needs it) for a node or group that has a schedule now turns that schedule off, after you confirm by typing the name; its rules are kept, and switching back to "Schedule, plus wake outside it" or "Off" lets you turn it on again. Before, the schedule kept starting and stopping it, so the two options behaved the same. The command line (`wake-policy --mode wake --disable-schedule`) and the API (`"disable_schedule": true`) refuse without that confirmation, and a schedule can't be turned on, nor a node set to Wake on demand join a scheduled group, while Wake on demand is set. Settings made with earlier versions keep working as before.
- The dashboard asks before every action that creates or deletes Linode instances: **Start** (an instance) and **Start group** say billing starts and what stops it again; **Stop group** needs the group name typed, as instance Stop already did; **Wake now** and **Wake the group now** ask first. The command line and API are unchanged.

## v1.21.2

### Changed
- The "service is starting" page shown through a NodeBalancer now has a progress bar with a percentage that follows the real start (the services it needs first, then the service itself, server by server), the current step, a live elapsed time, an estimate from the last wake, and a tiny jump-the-bugs game to play while waiting. It is a single light page (about 4 KB, nothing loaded from elsewhere), names no servers, and opens the service by itself when it's ready.

## v1.21.1

### Changed
- Activity now records who woke a node or group through a NodeBalancer: the visitor's address, the page it asked for and its browser (or "Start pressed" for a click), so a wake by a bot or scanner can be told apart from a real visitor.

### Fixed
- A group woken through a NodeBalancer (and the groups it depends on) got the 2-hour manual-start timer, which would stop it when the timer ran out even while visitors were using it. Woken members are now governed only by the idle stop, as a single woken node already was.

## v1.21.0

### Added
- Wake through a NodeBalancer opens the scheduler host's firewall by itself: one rule on the host's Cloud Firewall (`lis-wake-pages`, TCP 19200–19499 from the NodeBalancers' VPC subnets) is added when needed, kept in place and removed when nothing wakes through a NodeBalancer any more. Nothing else in the firewall is changed. Needs the Firewalls (Read/Write) token scope; `LIS_WAKE_MANAGE_FIREWALL=0` turns it off. `install.sh` opens the same range to private addresses in ufw or firewalld.
- A health alert (`wake-page`) when a NodeBalancer reports this host's starting page as down, so visitors can't wake anything through it.
- Dashboard: each NodeBalancer port in a Wake card has its own "Start on a click / Start on any request" switch, so an added port's trigger can be changed in place.

### Fixed
- `wake-nodebalancer` (and the dashboard) reported the wrong port after attaching when the node or group already had more than one port attached; the settings themselves were applied correctly.
- NodeBalancer membership: when a node using Linode Interfaces was stopped, Linode kept its NodeBalancer entries, pointing at a VPC address that is free while the node is stopped and could be given to another instance (which would then get the site's traffic). The tool now removes those entries right after the stop and adds them back after the next start, as it already did for configuration-profile interfaces. Entries left from earlier stops are removed by the scheduler within 5 minutes.

## v1.20.3

### Fixed
- Wake on demand: a node that had been woken and was then stopped by its schedule (or by hand) was still treated as woken after its next start, so it could be stopped for being idle shortly after its scheduled morning start and stay off for the rest of the day. A wake now ends at the node's next stop.
- Wake on demand: a node woken just before its schedule's hours could be stopped for being idle inside those hours, again leaving it off for the day. Inside an active schedule's hours the schedule now always decides, whichever wake mode is set.
- Stop: if an earlier stop had already powered the instance off (Linode sometimes takes longer than the tool waits), every retried stop tried to read the node's SSH keys from the powered-off instance and failed, so the instance was never deleted and kept billing. A stop of a powered-off instance now keeps the node's last-known SSH keys and skips its pre-stop hook.
- Dashboard: the Wake card's NodeBalancer section can now add several ports at once (for example 80 and 443). Ports already added are shown as such and the section stays open after adding.

## v1.20.2

### Changed
- Adding the deployment SSH key to the API token owner's Linode profile is now optional. A token without Account Read/Write (a least-privilege token) can't add profile SSH keys; the tool now notes this once in Activity instead of warning every hour, and `ssh-key-protect` shows the step as skipped. Nothing else changes: the key stays protected in the database, Object Storage and the passphrase's recovery copy, and a stopped node's SSH keys are recovered from Object Storage.

## v1.20.1

### Added
- `diagnose` matches Linode's own events to this tool's records. Every start and stop now records which Linode instance it created or deleted, and Linode's events for those instances appear in the timeline with who asked for them (for example "Linode: delete of instance 123 by instance-scheduler -- this product (stop by alice)"). A change by any other Linode user is reported as made outside the tool, however soon it followed one of the tool's own starts or stops; a change by the tool's own Linode user that the tool has no record of is reported too (another deployment, script or person using the same user or token).

### Changed
- Recommended setup: create the tool's API token from a dedicated Linode user, so Linode's events show the tool's changes apart from people's.

## v1.20.0

### Added
- `diagnose` (dashboard: "Investigate" next to a node's or group's "Why" line, and "Why?" on the off-schedule card): works out why a node or group is in its state, or why a pasted alert fired, from what was recorded -- start/stop history and failures, the activity log, when the scheduler was stopped or standing by, setting changes, start-order waits, holidays, and Linode's own events and the instance's live state for changes made in Cloud Manager (for example a node powered off there while this tool still records it as running). It names only causes it can prove, with the evidence and suggested next steps (one-click in the dashboard); otherwise it says it can't tell and shows the timeline. Ask answers "why" questions about named nodes, and pasted alerts, the same way.
- Setting change history: every change to a schedule, group, membership, start order, holiday, wake policy, manual-only setting, hooks, budget, guardrail, address or SSH port is recorded with the old and new value, who made it and when, for 90 days, backed up to Object Storage and restored by `restore`/`rebuild`. `changes`, `GET /changes`, and a "Setting changes" card on node and group pages.
- "Leave as is" on the off-schedule card (`schedule-catch-up --dismiss`, `POST /schedule/missed/dismiss`): the listed nodes are no longer listed or alerted on for that missed time and follow their schedule again from its next start or stop.
- Token scopes `system:diagnose` and `changes:read` (both in the `read` bundle).

### Fixed
- Changing a schedule (a node's own, its group's, its group membership, or the holidays) no longer makes the nodes look as if they missed a start or stop from before the change: the old schedule was in force then, so no "off schedule" alert is raised for it.
- `why` names a schedule edit made after a running node's stop time instead of calling it a missed stop.
- `wake-policy --name X` (or `--group-name`) without `--mode` now shows the current policy. It used to set it to `wake`.
- The catch-up API routes answer 400, not 500, for a malformed node name.

## v1.19.2

### Fixed
- Wake-on-demand settings (policies, wake links, NodeBalancer attachments and remembered backends), budgets and guardrails, and holidays are now kept in Object Storage one record per setting instead of one record per kind. Before, a change made on a new host before its database was restored (for example creating a wake link) replaced the stored copy with only what that empty database held, and a later restore removed every other setting. A restore now merges setting by setting: the newer copy wins, a setting deleted after the snapshot stays deleted, and nothing the database already has is removed. Settings saved by earlier versions are still read. `doctor` checks all three and `doctor --fix` re-uploads any that are missing.
- Ask answers a question that names a feature ("What is budgets?", "how do I use wake links", "when should I use hibernate") or matches an answer's title directly, instead of sometimes saying it wasn't sure.
- When Ask isn't sure and Smart Ask is set up, the suggestions now show at once; Smart Ask's answer or pick replaces them when it's ready, instead of the page waiting for Smart Ask with nothing shown.

## v1.19.1

### Changed
- The README describes the product as a whole -- schedules, groups and start order, hooks, wake on demand, manual control, costs and recovery -- with general walkthroughs (an office-hours development environment, an application that needs its database first, a tool woken on demand, standbys). Examples use generic node names, and the onboarding form's example name is `app-1`.

### Fixed
- Smart Ask: after an upgrade or a restart of its service, the first question waited while the model loaded (about 10 seconds on 8 vCPUs). The API server now keeps the model loaded and its fixed instructions read, checking every minute.
- Ask: answers written after the installed question model was built couldn't be found by it; they are now added to its index when it loads. New answer: what Create wake link does.
- Ask names Smart Ask instead of the model behind it ("Smart Ask thinks the first one fits best").
- Wake on demand: Activity now says when a woken node can't be judged yet (Linode has no statistics for an instance's first minutes), and, before every idle stop, what it was idle since -- a node whose idle minutes ran out before its first figures arrived used to stop with no line explaining it.

## v1.19.0

### Added
- `why`: says in one line why a node is running or stopped right now (its schedule and whether it is inside the hours, a holiday, an extension or a manual start's auto-stop, wake on demand, a start or stop waiting on a group dependency, manual-only, hibernation), who last started or stopped it, and when it changes next. `--group-name` rolls a group's members up. Also `GET /instances/{name}/why`, `GET /groups/{name}/why`, a **Why** line on the dashboard's instance page, a **Now** card on the group page, and the chat `why <node>` command; `status` prints the same line.
- Wake on demand: Activity now also says when a woken node stops being used and when it will stop unless used again, and when a `schedule-wake` node is left to its schedule inside its hours.

### Fixed
- Ask's "why is it running" answer said "its group's schedule" for a node following its own schedule.

## v1.18.1

### Fixed
- Wake on demand's idle stop: a woken node that had been busy (NodeBalancer visitors, CPU or network) was kept running for good, because the same busy reading counted again at every check. A node now stops once its idle minutes have passed after its last use; since Linode fills in its newest figures late, use counts until 15 minutes after the last busy reading.
- Waking through a NodeBalancer: the NodeBalancer's figures give the number of connections in each 5-minute sample, which was read as connections per second, so a few stray requests counted as visitors. The limit (`LIS_WAKE_NB_BUSY_CONNECTIONS`, 5 per second) now applies to the real rate.
- Two scheduler deployments sharing one SSH key on the same Linode account replaced each other's recovery copy of the key's passphrase; each now keeps its own.
- Wake on demand: an open SSH terminal session wasn't seen on systems whose `who` lists the session type before the terminal (Debian 13), so a node could be stopped while someone was logged in.
- Activity names a wake from a NodeBalancer's starting page as such, not as a wake link.

## v1.18.0

### Added
- Wake through a NodeBalancer: visitors to a site's normal address wake its node or group. The scheduler host joins one port of a VPC NodeBalancer as a backup node, which serves a "the service is starting" page only while the real backends are down, and starts them on a click (or any request, with a cooldown); the NodeBalancer switches back by itself once they pass their health checks. Set up per node or group (`wake-nodebalancer`, or the Wake card); recovered from Object Storage and the NodeBalancer's own tags.
- NodeBalancer membership: Linode removes an instance from its NodeBalancers when it is deleted, which every stop does. Before each stop the tool now records every VPC NodeBalancer node at the node's VPC address (label, IP address and port, mode, weight, VPC subnet, and the port's settings for reference) and adds each back right after the next start, with or without wake on demand. A node removed from a NodeBalancer while running isn't put back. New `nb-membership` command, a NodeBalancer membership card on the instance page and `GET`/`DELETE /instances/{name}/nodebalancer-membership`. A stop is refused if the NodeBalancers can't be read (`--force` overrides).
- Wake on demand for groups in the dashboard (a Wake card on the group page) and `wake --group-name`: a group wakes with the groups it depends on first.
- `nb-membership`: lists the VPC NodeBalancers a node is added back to after each start (with the NodeBalancer and port settings recorded at the stop), and `--forget` stops adding one back. Also on the instance page and in the API.
- `rebuild --name <node>` (repeatable; `names` in the API) recovers only the named nodes and leaves everything else found on the account alone.
- Ask answers questions about Linode's own services as this tool handles them (onboarding a NodeBalancer, a Managed Database or an LKE cluster, storage, Linode Backups) and about every configuration setting: what it does, where it is set, how to change it and whether a restart is needed.

### Changed
- Smart Ask: its pick is listed first among the suggestions, starred and with a one-line reason, instead of being shown as the answer, so a wrong pick never reads as certain. It also reads a shorter prompt, about twice as fast on a CPU-only host.
- Ask's question model is updated, and corrects common misspellings and joined forms of product words ("on-board", "node balancer").
- The Linode API token needs NodeBalancers (Read/Write) to keep nodes in their NodeBalancers across stop and start; without it a stop warns and continues.
- Wake on demand's idle stop: a node now counts as in use at 15% CPU (was 5%; an idle node booted from a volume reads 5-10% because Linode's figure includes I/O wait), and the levels can be changed in `.env` (`LIS_WAKE_IDLE_CPU_PCT`, `LIS_WAKE_IDLE_NET_BPS`, `LIS_WAKE_NB_BUSY_CONNECTIONS`). A NodeBalancer counts as busy only when two readings in a row (10 minutes) are over its limit, so short bursts of stray traffic don't keep a node up. Activity now says why a woken node is kept running.

### Fixed
- A replacement scheduler host could overwrite the backed-up trusted SSH host keys with its own before they were restored, so nodes that were stopped at the time failed their next start with a host-key error. The backup now keeps the keys of every node still managed, and a restore never adds a second key for an address already trusted.
- Group recovery tags: Linode treats tags as case-insensitive across the account, so a group's tag could be stored in another casing and every start and stop reported a failed tag write; recovery could also bring the group back under that casing. Tags are now compared without case, `rebuild` restores the group's real name from Object Storage, and group names that differ only in case are refused.
- Removing a node's wake-through-NodeBalancer attachment no longer forgets that node's NodeBalancer membership.

## v1.17.1

### Fixed
- `install.sh`: smart Ask's model server could never start on a fresh install (its folder was created readable by root only); re-running the installer repairs an existing install. When smart Ask can't be set up, the installer now shows why.
- `install.sh`: the dashboard domain question asks again when the answer isn't a domain name, instead of skipping HTTPS and showing a wrong dashboard address.

## v1.17.0

### Added
- The dashboard and the Definitive Guide have two mascots: Sol, the scheduler's sloth (logo, sign-in page, empty pages), and Glim, the help firefly (where the product explains an error, and Ask).
- Dashboard teams and roles. The Linode account's unrestricted users (and anyone in the new `LIS_ADMIN_USERS`) are admins; admins put everyone else into teams on the new Users & teams page (or with `team-create`, `team-add-member` and the other `team-*` commands). A team gives a role (viewer, operator, editor or admin) on all schedule groups and instances, or only on the ones listed, and can take in people automatically by the domain of their Linode email. Teams are backed up to Object Storage and come back with `restore` and `rebuild`. New API token scope `users:manage`.
- Budgets and guardrails. Monthly running-hour budgets per schedule group or instance (alert at 80% and 100%; at 100% optionally refuse manual starts and extensions), a longest-extension limit, a limit on instances running at once, quiet periods, and an optional nightly safety net that stops instances running outside their schedule (at the chosen hour in each instance's schedule timezone). Guardrails are set for all instances and can be set per schedule group, replacing the all-instances setting for the group's members. A refused start or extension can be sent to an admin for approval. New Budgets page, `budget-*`, `guardrail-set` and `approval-*` commands, exit code 6, and scopes `budgets:read`, `budgets:write`, `approvals:request`, `approvals:decide`. Scheduled starts are never refused.
- Health and cost insight. The tool keeps 30 days of its own health figures (start/stop times, Linode API latency, errors and rate limiting, scheduler check time, database lock waits, host memory/disk/load) and reports slow or high findings with the likely cause and the fix, as alerts too. New Health page, `insight` command and a Prometheus endpoint (`/metrics`). Right-sizing hints from Linode's own CPU figures. Optional, off by default: server metrics from node_exporter installed on a node or a group's nodes (loopback only, read over SSH), turned on with `metrics-agent` or on the instance page. New admin-only scope `metrics-agent:manage`.
- Usage-learned schedules: a suggested schedule for a node or group from the last 4 weeks of real use (Linode CPU and network per half-hour, plus manual starts), with margins, weekdays with no use left out, and a check that it would never have stopped a node while it was in use. Never applied by itself; apply and undo from the instance or group page or `schedule-suggest`.
- Wake on demand: a node or group can stay stopped until someone needs it (or be woken outside its schedule) and stop again after a set idle time, judged from Linode's own CPU/network figures and open SSH sessions. People wake it from the dashboard, with `wake`, or from the node's secret wake link (a page that starts it only when Start is pressed, then shows the address to connect to directly). New `wake-policy`, `wake-link`, `wake` commands and scope `wake:manage`.
- Slack and Microsoft Teams: alerts, approval requests (with Approve/Deny buttons in Slack) and an optional daily summary in chat; `/scheduler` (Slack, over Socket Mode -- no open port) and `@Scheduler` (Teams) commands for status, savings, why, start, stop, extend and approvals. Acting from chat needs the chat user linked once to a dashboard user, whose role and teams then apply.
- Hibernate: move a node stopped for a long time to Object Storage (compressed, every chunk checked after upload; the volumes are kept for the deletion grace period, then deleted) and bring it back with `unhibernate`; the reserved IP is kept; after `unhibernate` the copy is kept for the grace period, then deleted. Never automatic -- the name must be typed, and nodes stopped 30+ days are only suggested. New `hibernate`/`unhibernate` commands, a card on the instance page and scope `instances:hibernate`.
- Ask is back as help search: questions in your own words are matched against the product's own reviewed answers by a small language model shipped with the release and run on the host's CPU (nothing sent anywhere). It answers directly only when very sure; otherwise it lists up to five answers that may help. On when the model is installed; `LIS_ENABLE_ASK=0` turns it off.
- Smart Ask: when the instant answer isn't sure, an open model on this host picks the best of the closest answers. `install.sh` sets it up by the host's memory (16 GB or more: Qwen 2.5 7B; 8 GB: Qwen3 4B; smaller: off) as a low-priority service, `linode-scheduler-ollama`; `--no-smart-ask` turns it off. It only chooses among the reviewed answers and nothing leaves the host.

### Changed
- Upgrading: restricted users of the Linode account no longer get full dashboard access on their own. After upgrading they see a "waiting for access" page until an admin adds them to a team. Unrestricted users keep full access.

### Security
- Database snapshots in Object Storage are encrypted (AES-256-GCM) with the deployment passphrase (`LIS_SSH_KEY_PASSPHRASE`); restoring needs it, and a replacement host gets it from your Linode account. Older unencrypted snapshots still restore.
- Hibernated disks are encrypted: each hibernation gets its own random key, every chunk is encrypted before upload, and the key is stored only sealed with the deployment passphrase.
- Dashboard sign-in sessions are stored only as hashes, so a database snapshot holds no working sign-in. Existing sessions keep working.

## v1.16.3

### Security
- Dashboard sign-in is limited to users of the Linode account this deployment manages. A Linode login proves who someone is but not that they belong to your account; the tool now checks the username against your account's users (read with `LINODE_API_TOKEN`, which needs Account (Read Only) for this, as the setup steps already ask) and refuses anyone else, showing the reason on the sign-in page. Signed-in users are checked again every 15 minutes, so removing someone from the account also ends their dashboard access.
- New optional setting `LIS_DASHBOARD_USERS`: a comma-separated list of Linode usernames allowed to sign in. It narrows access further, and is required if your API token has no Account access (without either, nobody can sign in to the dashboard; the CLI and API tokens are unaffected).
- Everyone signed in before this release is asked to sign in once more, so every session has passed the check.

### Fixed
- `explain` picks the more specific explanation when several match: a Lish console "Permission
  denied" is explained as Lish console access (not an SSH login to the node), and recovery-drill,
  "could not capture the instance" and ambiguous-create errors get their own explanations. It also
  recognises "recovery-drill:" and "could not capture" as written in the tool's messages.

## v1.16.1

### Changed
- The free-form `ask` command, its API routes (`/system/ask...`) and the dashboard's Ask page and
  Ask card are withdrawn: its answers weren't reliable enough for every way a question can be
  worded. `explain` (an error's cause, read-only checks and what to do, also "Explain this" on
  every dashboard error) and `doctor --fix` are unchanged. API tokens holding the `system:ask`
  scope keep working for their other scopes.

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
