# Live infrastructure testing log

This file lives only in this repo — it is never generated or overwritten by the
publish pipeline (`scripts/push_customer_repo.sh` in the dev repo preserves it across every
release). It's a running, hand-maintained record of end-to-end testing done against this
exact repo, deployed for real against a real Linode account, not a code review.

## Round 1 — 2026-08-29 through 2026-09-17

Full end-to-end live deployment test against `v1.2.0`, exercising the complete customer journey:
fresh clone, real `.env` setup, real image-deployed instances with real data volumes taken
through the documented Path B migration (including the manual Rescue Mode `dd` copy — twice,
once deliberately restarted mid-flight to exercise `migrate-start --force`'s local-state-loss
recovery), `onboard`, a real stop/start cycle (data survived byte-for-byte, SSH host key stayed
stable), individual and group scheduling actually firing in real time via `poll --once`, manual
override arm/extend/auto-revert, `rebuild` against a genuinely deleted local database,
`clear-lock`, a real SSH host-key rotation through `reset-host-key`, `migrate-orphans`
(list/`--mark-orphaned`/`--cleanup`) against a real orphaned volume, `offboard --delete-volumes`
and `deregister` each confirmed via direct API reads, and a real registered "Login with Linode"
OAuth Client.

**Found 2 release-blocking bugs, both in the publish pipeline itself (not the product code) —
fixed and shipped as `v1.2.1`:**

1. Every `start`/`stop` crashed, 100% of the time. A stray `@contextmanager` decorator ended up
   attached to the wrong function during the export process.
2. The web dashboard could never load, no matter how correctly the documented build steps were
   followed. A path the server used to find the built dashboard pointed one directory too high.

Both root-caused precisely, fixed at the source, and verified via a full re-export plus a
fresh-venv smoke test before publishing `v1.2.1`.

**What was not finished in round 1** (carried into round 2): the REST API endpoint sweep beyond
basic auth checks, and a full headless-browser walkthrough of the web dashboard's own pages.

**Since round 1**: publishing this repo is now automated — pushing a `v*.*.*` tag to the dev repo
runs a CI pipeline (export → structural safety gate → push) instead of a manual process. The
safety gate includes direct regression tests for both bugs found in round 1, so they can't ship
again silently.

## Round 2 — 2026-09-17 through 2026-09-18, complete

Continuing live testing against `v1.2.1`, this time using a dedicated API token
(`instance-scheduler-live-testing`, scoped full-access, isolated from any other credential) and
covering every remaining command and both the full REST API surface and the dashboard's own
pages via a real headless browser — the two pieces round 1 didn't finish. Any bug found gets
fixed in the dev repo, released, and re-tested, looping until a full pass finds nothing.

**Coverage so far**: every local-only command; a real 2-node onboard through the full Path B
migration (`migrate-start`/manual `dd`/`migrate-resume`); `migrate-orphans`; and, deliberately
before moving to scale, a real OS-diversity pass — 13 separate real instances, spanning Ubuntu
20.04/22.04/24.04/25.10, Debian 11/12/13, AlmaLinux 8/9/10, Rocky 9, CentOS Stream 9, Fedora 43,
and openSUSE 15.6, each taken through the real data-volume setup and Path B migration. 10 of 13
migrated and onboarded cleanly; Debian 11/12 and openSUSE 15.6 were correctly refused by the
tool's own cloud-init version pre-flight gate (not a bug — the gate working as designed, with
clear upgrade guidance). One real, self-inflicted device-mismatch during this pass (a manual API
boot call that booted the wrong config) was caught cleanly by the tool's own root-device
verification, exactly the safety mechanism it exists for.

**Found 1 real, customer-impacting bug — fixed and shipped as `v1.2.2`:**

Every `--ssh-key` default (`migrate-start`/`migrate-resume`/`onboard`/`start`/`stop`/`poll`/
`reset-host-key`/`rebuild`) silently ignored `LINODE_SSH_KEY_PATH` from `.env`, despite
`.env.example`'s own comment claiming it worked the same way the API server already does.
Reproduced live: a real `poll --once` run — run exactly the way an unattended cron/systemd job
would, with no `--ssh-key` flag — failed 4 of 7 scheduled stops in a real schedule group with a
genuine SSH permission error, while a manual stop with the key explicitly passed succeeded
seconds later. Fixed at the root (the CLI now loads `.env` before computing any `--ssh-key`
default, matching the API server's own correct order) and verified twice: once directly against
the fix's own logic, and once for real — the exact failing scenario (a real, unattended
`poll --once`, zero `--ssh-key` overrides) re-run against `v1.2.2` on the same real fleet,
successfully firing 11 real stop/start actions across two schedule groups (7 and 6 members, two
different timezones) with zero failures. Individual-schedule override precedence (an instance
with its own schedule correctly ignoring its group's) held correctly throughout.

**Remaining coverage, completed after the fix above:**

- **Manual override, extend, auto-revert**: all three confirmed live on a real instance —
  arming (`start` outside a schedule's on-window correctly prints the auto-stop time and sets
  the timer), `extend` (pushes the timer forward, confirmed via `status`), and auto-revert
  itself (a short 2-minute window, confirmed the instance genuinely stopped via `poll --once`
  once it expired).
- **Groups, full CRUD**: `group-create`, `group-schedule-set`, `group-add`/`group-remove` (both
  `--copy-schedule` and `--keep-manual` paths confirmed — a removed member correctly either
  gained the group's rules as its own individual schedule, or ended up purely manual with no
  schedule at all), `group-show`, `group-list`, `group-delete`, all against 2 real schedule
  groups (7 and 6 members, different timezones).
- **Disaster recovery at real fleet scale**: the local registry database was deleted outright
  a second time (13 real instances, 2 real groups, 2 individual schedules this time, not just a
  single node) and `rebuild` reconstructed everything byte-for-byte from Linode's own tags —
  both groups recreated from member tags, both individual schedules restored, every running
  instance fully recovered, every stopped instance correctly flagged for the documented manual
  recovery step (boot once, then re-`onboard`). Spot-checked one recovered record's
  `os_volume_id`/`reserved_ip`/`group_id` against a pre-loss snapshot — exact match. Group
  membership was also confirmed to survive an `onboard --force` re-onboard cleanly afterward.
- **`clear-lock`/`reset-host-key`**: both `--name` and `--ip` modes of `reset-host-key`
  confirmed working against a real instance.
- **`offboard`/`deregister`**: `offboard` (default, keeping volumes) confirmed — the reserved
  IP was independently confirmed released back to Linode's pool via a direct API read (404
  afterward). `deregister` confirmed — the real Linode instance was independently confirmed
  completely untouched afterward, then re-`onboard`ed back in cleanly.
- **Every documented REST API endpoint**, using a real running `serve-api` process and a
  directly-issued session token (the interactive "Login with Linode" step itself needs the
  account owner's own password to complete, and was already live-verified once in this
  project's history — see the note in `README.md`/`DEPLOYMENT.md`): every group and instance
  CRUD endpoint, schedule set/clear, group membership PATCH, the savings endpoints (values
  matched hand-computed expectations exactly, e.g. a daily 7h21m on-window correctly showing
  69.4% savings), `history`, `/linode/ssh-check` (including its SSRF guard — a cloud metadata
  address was correctly rejected with a clean 422, a real managed instance's IP correctly
  returned reachable), the background-operation pattern for `start`/`stop` (polled to
  completion with real incrementing progress), `/health`, `/logout` (confirmed the same token
  is rejected immediately afterward), and a full onboard → stop → `offboard --delete-volumes`
  cycle driven entirely through raw HTTP calls, independently confirmed via direct API reads
  that both the volume and the reserved IP were genuinely gone afterward.
- **The web dashboard**, via a real headless-Chromium pass against the real running server (no
  mocking) — Instances list, Groups list, group detail (correct member list, correct schedule
  editor, correct savings percentage), instance detail (correct volumes, correct "following
  group" schedule display, a real Stop button correctly triggering its two-stage
  type-to-confirm flow), and the Onboard page (correctly listing every real raw Linode instance
  on the account, filterable by region/tag). Zero page errors and zero browser console errors
  across the entire pass. One apparent issue (the very first page load briefly showing the
  login screen even with a session token present) was investigated directly and confirmed to be
  an artifact of the test script's own token-injection timing, not a real bug — a corrected
  test with the token present from the very first render loaded the authenticated page
  immediately, matching exactly how the real OAuth redirect delivers it in production.

**Round 2 complete: one real bug found, fixed, shipped, and re-verified live (`v1.2.2`); no
other bugs found across the full remaining command/API/dashboard surface.** The live fleet used
for this round (12 real instances spanning 10 different OS/version combinations, 2 real schedule
groups) was left running as known-good reference infrastructure rather than torn down.

*(Future rounds append here.)*
