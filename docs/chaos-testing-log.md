# chaos-testing-log.md

## Kickoff — chaos monkey infrastructure validated, starting now

## Chaos round — 2026-09-18T04:59:26.016732+00:00

**Scenario**: `lock_contention` — target: `r2-os-ubuntu2004`
**Result**: FINDING — outcome: `FINDING`

```
{'stop_while_locked_stdout': '', 'stop_while_locked_stderr': "Configuration error: Another start/stop/rebuild/clear-lock process is already operating on 'r2-os-ubuntu2004' right now. Wait for it to finish and try again.\n"}
```

## Correction — 2026-09-18

The `lock_contention` FINDING immediately above (2026-09-18T04:59:26Z) was a bug in the
chaos-monkey harness itself, not the product, found and fixed within the hour: the
long-running soak harness's own daily disaster-recovery drill fired immediately at startup
(an uninitialized "last run" timestamp made the first interval look overdue on the very
first loop iteration) and raced against this exact chaos round, which happened to be running
against a different instance at the same moment -- the two harnesses mutating/reading the
shared registry concurrently produced confusing, hard-to-attribute output. Manually reproducing
the identical scenario in isolation (no concurrent drill) confirmed the real product behavior
is correct: a held per-instance lock is refused cleanly ("Configuration error: Another
start/stop/rebuild/clear-lock process is already operating..."), and normal operation resumes
immediately once the lock is released. Fixed with a shared file-based mutex so the two
harnesses' registry-level operations can never run concurrently again, plus a calm startup
delay on both. The live fleet was independently confirmed fully healthy throughout -- no
instance was ever actually harmed by this.

## Chaos round — 2026-09-18T05:05:12.526811+00:00

**Scenario**: `out_of_band_delete` — target: `r2-node-2`
**Result**: Confirmed working as documented — outcome: `reset_to_stopped_cleanly`

```
{'stop_returncode': 0, 'stop_stdout_tail': "'r2-node-2': instance 105592839 no longer exists (confirmed 404, presumably deleted out-of-band) -- reset to 'stopped'. Its OS volume, data volume(s), and reserved IP are unaffected.\n", 'recreated': False}
```

## Non-finding, investigated and fixed in the harness — 2026-09-18T05:12Z

The first real autonomous round after the coordination fix above (`out_of_band_delete` against
`r2-node-2`) worked exactly as intended for the thing actually under test: an out-of-band
delete via the raw Linode API, followed by `stop`, correctly hit the documented confirmed-404
recovery path (gotchas #48/#50) and reset the record to `stopped` cleanly, with the OS volume,
data volume(s), and reserved IP all confirmed unaffected.

The round's own best-effort convenience step -- a `start` call at the end meant to bring the
instance back up before the next round, not itself the thing being tested -- reported
`recreated: false`. Investigated rather than assumed: the whole round (inject-delete, an 8s
sleep, `stop`, a status check, `start`) completed in about 16 seconds total. A real create +
boot + SSH-verify cycle for this exact instance was independently timed, moments later, at over
a minute -- so the original `start` attempt could not have genuinely tried and failed a real
create; it almost certainly hit a fast, clean lock-contention refusal from the real poller
(running continuously in the background against the same fleet) briefly holding this same
instance's per-instance lock at that exact moment. That is correct, by-design fail-fast
behavior (documented and intentional -- concurrent operations on the same instance are meant to
refuse immediately, never block), not a product bug.

Manually running `start` again for `r2-node-2` moments later succeeded normally (created,
booted, verified reachable in a little over a minute) and correctly armed a manual-override
auto-revert timer, since 05:05 UTC falls outside all of `dev-fleet-a`'s configured on-windows --
exactly the intended manual-start-outside-schedule behavior.

Fixed in the harness (not the product): the recovery `start` step now retries once after a
short pause if the first attempt fails, so a fleet member isn't left needlessly stopped for the
rest of a long unattended run just because it raced the poller's own tick once. The scenario's
actual pass/fail signal (the confirmed-404 reset) was never affected by this either way -- it's
correctly evaluated and reported independently of the recovery step's own success.

## Chaos round — 2026-09-18T05:12:32.109791+00:00

**Scenario**: `poller_crash_mid_tick` — target: `n/a`
**Result**: Confirmed working as documented — outcome: `clean_retry_no_stuck_locks`

```
{'retry_returncode': 0, 'retry_stdout_tail': 'Tick complete: 0 fired, 0 failed, 12 instance(s) checked.\n', 'stuck_instances': []}
```
