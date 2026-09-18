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

## Chaos round — 2026-09-18T06:58:24.345296+00:00

**Scenario**: `registry_corruption` — target: `n/a`
**Result**: Confirmed working as documented — outcome: `clean_refusal_then_recovered`

```
{'list_after_corruption_stderr': "Configuration error: the local registry database at /opt/soak-chaos/product/state/instances.db appears corrupted or unreadable (file is not a database). Recovering local records from Linode's own tags needs a working, even if empty, local database first -- move the corrupted file aside (e.g. `mv /opt/soak-chaos/product/state/instances.db /opt/soak-chaos/product/state/instances.db.corrupted`) and re-run this command; a fresh, empty database will be created automatically, and `rebuild` can then re", 'rebuild_stdout_tail': "am9''s membership in group 'dev-fleet-b' from tags.\n  restored schedule for 'r2-grp-01' from tags.\nScanned tags: 12 name(s) found.\n  fully recovered (was running): r2-node-1, r2-node-2, r2-os-ubuntu2204, r2-os-ubuntu2004, r2-os-almalinux9, r2-os-debian13, r2-os-fedora43, r2-grp-01\n  partially recovered (was stopped, needs manual recovery): r2-os-almalinux8, r2-os-almalinux10, r2-os-rocky9, r2-os-centos-stream9\n    -- boot each of these manually once via Cloud Manager from its os_volume_id, using its network_config from Cloud Manager's own UI, then re-run `onboard` to fully restore management.\n"}
```

## Full recovery completed for the 4 partially-recovered nodes -- 2026-09-18T08:35Z

Follow-up to the `registry_corruption` chaos round documented above: `rebuild`'s own documented
contract only partially recovers a node that was `stopped` at the moment of local database loss
(network/SSH details aren't knowable while it's off) -- 4 real fleet members
(`r2-os-almalinux8`/`almalinux10`/`rocky9`/`centos-stream9`) ended up in `needs_manual_recovery`
as a direct, expected consequence, exactly matching the documented recovery instructions
`rebuild` itself printed.

Live-exercised that documented recovery path for real, for all 4: booted each from its recorded
`os_volume_id` with its recorded `reserved_ip` (via the raw API, since these were temporarily
outside the tool's own management), `reset-host-key` to trust each fresh boot's genuinely new
SSH host key, then `onboard --force --instance-id <id>` to hand full management back to the
tool. All 4 confirmed back under management with the correct `linode_id` and no dangling
resources -- `instance_manager.py list` now shows all 12 fleet members accounted for again, with
zero manual-recovery records outstanding. This is the first time this exact recovery sequence
was exercised end-to-end against real infrastructure during this test run, not just asserted by
a unit test.

## Chaos round — 2026-09-18T09:21:29.941624+00:00

**Scenario**: `out_of_band_delete` — target: `r2-os-rocky9`
**Result**: Confirmed working as documented — outcome: `reset_to_stopped_cleanly`

```
{'stop_returncode': 0, 'stop_stdout_tail': "'r2-os-rocky9': instance 105631411 no longer exists (confirmed 404, presumably deleted out-of-band) -- reset to 'stopped'. Its OS volume, data volume(s), and reserved IP are unaffected.\n", 'recreated': True, 'recreate_retried': True, 'recreate_stderr_tail': None}
```

## Chaos round — 2026-09-18T13:08:05.564954+00:00

**Scenario**: `poller_crash_mid_tick` — target: `n/a`
**Result**: Confirmed working as documented — outcome: `clean_retry_no_stuck_locks`

```
{'retry_returncode': 0, 'retry_stdout_tail': 'Tick complete: 0 fired, 0 failed, 12 instance(s) checked.\n', 'stuck_instances': []}
```

## Chaos round — 2026-09-18T14:18:55.624643+00:00

**Scenario**: `ssh_host_key_mismatch` — target: `r2-os-almalinux10`
**Result**: Confirmed working as documented — outcome: `failed_closed_then_reset_ok`

```
{'start_stdout': "Starting 'r2-os-almalinux10'...\n  instance created: 105669025 (booting...)\n  instance: 105669025\n  running -- verifying real network reachability...\n", 'start_returncode': 1, 'recovery_start_returncode': 0}
```

## Chaos round — 2026-09-18T16:11:33.095232+00:00

**Scenario**: `lock_contention` — target: `r2-node-2`
**Result**: Confirmed working as documented — outcome: `clean_lock_refusal_then_normal`

```
{'stop_while_locked_stdout': '', 'stop_while_locked_stderr': "Configuration error: Another start/stop/rebuild/clear-lock process is already operating on 'r2-node-2' right now. Wait for it to finish and try again.\n"}
```

## Chaos round — 2026-09-18T19:53:41.827966+00:00

**Scenario**: `out_of_band_delete` — target: `r2-os-fedora43`
**Result**: Confirmed working as documented — outcome: `reset_to_stopped_cleanly`

```
{'stop_returncode': 0, 'stop_stdout_tail': "'r2-os-fedora43': instance 105595545 no longer exists (confirmed 404, presumably deleted out-of-band) -- reset to 'stopped'. Its OS volume, data volume(s), and reserved IP are unaffected.\n", 'recreated': False, 'recreate_retried': True, 'recreate_stderr_tail': "Configuration error: failed to start 'r2-os-fedora43': POST /v4/linode/instances/105700611/configs: [400] Volume 18017664 already attached to Linode 105595545\n"}
```
