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

## Round 2 — 2026-09-17, in progress

Continuing live testing against `v1.2.1`, this time using a dedicated API token
(`instance-scheduler-live-testing`, scoped full-access, isolated from any other credential) and
covering every remaining command and both the full REST API surface and the dashboard's own
pages via a real headless browser — the two pieces round 1 didn't finish. Any bug found gets
fixed in the dev repo, released, and re-tested, looping until a full pass finds nothing.

*(Updated as testing proceeds — see the section below for the current pass's own findings.)*
