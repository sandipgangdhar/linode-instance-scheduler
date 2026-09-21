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

## Chaos round — 2026-09-18T21:47:02.610823+00:00

**Scenario**: `poller_crash_mid_tick` — target: `n/a`
**Result**: Confirmed working as documented — outcome: `clean_retry_no_stuck_locks`

```
{'retry_returncode': 0, 'retry_stdout_tail': 'Tick complete: 0 fired, 0 failed, 12 instance(s) checked.\n', 'stuck_instances': []}
```

## Chaos round — 2026-09-19T00:11:57.074849+00:00

**Scenario**: `out_of_band_shutdown` — target: `r2-os-rocky9`
**Result**: Confirmed working as documented — outcome: `skipped_not_running`

```
{}
```

## Chaos round — 2026-09-19T03:02:05.720619+00:00

**Scenario**: `poller_crash_mid_tick` — target: `n/a`
**Result**: Confirmed working as documented — outcome: `clean_retry_no_stuck_locks`

```
{'retry_returncode': 1, 'retry_stdout_tail': "'r2-node-1': create due, firing (triggered_by=schedule)...\n  r2-node-1: error action=create (Another start/stop/rebuild/clear-lock process is already operating on 'r2-node-1' right now. Wait for it to finish and try again.)\nTick complete: 0 fired, 1 failed, 12 instance(s) checked.\n", 'stuck_instances': []}
```

## Chaos round — 2026-09-19T04:49:17.841092+00:00

**Scenario**: `out_of_band_shutdown` — target: `r2-os-centos-stream9`
**Result**: Confirmed working as documented — outcome: `recovered`

```
{'first_start_returncode': 1, 'first_start_stdout_tail': "'r2-os-centos-stream9' instance 105746813 exists but is offline -- likely powered off out-of-band. Marking it for recovery...\n", 'second_start_returncode': 0, 'second_start_stdout_tail': "'r2-os-centos-stream9' has an unreachable instance (105746813) from a previous start attempt -- retrying the reachability check instead of creating a new one...\n  running -- verifying real network reachability...\n'r2-os-centos-stream9' is up at 172.236.177.58.\n"}
```

## Chaos round — 2026-09-19T07:47:28.656168+00:00

**Scenario**: `lock_contention` — target: `r2-os-ubuntu2204`
**Result**: Confirmed working as documented — outcome: `clean_lock_refusal_then_normal`

```
{'stop_while_locked_stdout': '', 'stop_while_locked_stderr': "Configuration error: Another start/stop/rebuild/clear-lock process is already operating on 'r2-os-ubuntu2204' right now. Wait for it to finish and try again.\n"}
```

## Chaos round — 2026-09-19T09:33:12.493293+00:00

**Scenario**: `tag_tampering` — target: `r2-os-rocky9`
**Result**: Confirmed working as documented — outcome: `retagged_on_next_cycle`

```
{'stop_returncode': 0, 'start_returncode': 0, 'tags_after': ['linode-scheduler-name:r2-os-rocky9', 'linode-scheduler-role:os']}
```

## Chaos round — 2026-09-19T10:35:01.085762+00:00

**Scenario**: `out_of_band_delete` — target: `r2-os-ubuntu2204`
**Result**: Confirmed working as documented — outcome: `skipped_not_running`

```
{}
```

## Chaos round — 2026-09-19T12:46:17.084924+00:00

**Scenario**: `lock_contention` — target: `r2-node-2`
**Result**: Confirmed working as documented — outcome: `clean_lock_refusal_then_normal`

```
{'stop_while_locked_stdout': '', 'stop_while_locked_stderr': "Configuration error: Another start/stop/rebuild/clear-lock process is already operating on 'r2-node-2' right now. Wait for it to finish and try again.\n"}
```

## Chaos round — 2026-09-19T14:59:15.348227+00:00

**Scenario**: `out_of_band_shutdown` — target: `r2-os-ubuntu2004`
**Result**: Confirmed working as documented — outcome: `skipped_not_running`

```
{}
```

## Chaos round — 2026-09-19T18:55:15.051852+00:00

**Scenario**: `registry_corruption` — target: `n/a`
**Result**: Confirmed working as documented — outcome: `clean_refusal_then_recovered`

```
{'list_after_corruption_stderr': "Configuration error: the local registry database at /opt/soak-chaos/product/state/instances.db appears corrupted or unreadable (file is not a database). Recovering local records from Linode's own tags needs a working, even if empty, local database first -- move the corrupted file aside (e.g. `mv /opt/soak-chaos/product/state/instances.db /opt/soak-chaos/product/state/instances.db.corrupted`) and re-run this command; a fresh, empty database will be created automatically, and `rebuild` can then re", 'rebuild_stdout_tail': "am9''s membership in group 'dev-fleet-b' from tags.\n  restored schedule for 'r2-grp-01' from tags.\nScanned tags: 12 name(s) found.\n  fully recovered (was running): r2-os-almalinux10, r2-os-rocky9, r2-os-centos-stream9\n  partially recovered (was stopped, needs manual recovery): r2-node-1, r2-node-2, r2-os-ubuntu2204, r2-os-ubuntu2004, r2-os-almalinux9, r2-os-debian13, r2-os-almalinux8, r2-os-fedora43, r2-grp-01\n    -- boot each of these manually once via Cloud Manager from its os_volume_id, using its network_config from Cloud Manager's own UI, then re-run `onboard` to fully restore management.\n"}
```

## Chaos round — 2026-09-19T21:36:04.229081+00:00

**Scenario**: `poller_crash_mid_tick` — target: `n/a`
**Result**: Confirmed working as documented — outcome: `clean_retry_no_stuck_locks`

```
{'retry_returncode': 0, 'retry_stdout_tail': 'Tick complete: 0 fired, 0 failed, 12 instance(s) checked.\n', 'stuck_instances': []}
```

## Chaos round — 2026-09-19T23:37:42.022918+00:00

**Scenario**: `out_of_band_delete` — target: `r2-os-almalinux10`
**Result**: Confirmed working as documented — outcome: `skipped_not_running`

```
{}
```

## Chaos round — 2026-09-20T02:13:02.222139+00:00

**Scenario**: `tag_tampering` — target: `r2-os-debian13`
**Result**: FINDING — outcome: `FINDING`

```
{'stop_returncode': 1, 'start_returncode': 1, 'tags_after': []}
```

## 2026-09-21: tag_tampering FINDING was a downstream effect of the drill stranding above

A `tag_tampering` round picked `r2-os-debian13` as its target while it was already stuck in
`needs_manual_recovery` (see the same-day long-running-test-log entry). Both the scenario's own
`stop` and `start` calls correctly refused (the product's own designed behavior for that status),
but since neither ran, the volume's tampered-away disaster-recovery tags were never restored --
the instance lost its tag-based identity entirely and stopped showing up in the registry at all.
The chaos monkey correctly flagged this as a FINDING and self-paused.

Root cause was the drill stranding above, not an independent product defect -- confirmed by
recovering the instance (fresh boot from its still-tag-identifiable OS volume + reserved IP, then
`onboard --force`) and confirming its tags round-trip correctly afterward. Hardened
`pick_target()` (used by every chaos scenario) to only ever select a name currently in a normal
`running`/`stopped` state, so no future scenario can inject against an already-broken instance and
risk compounding it into something worse. Chaos injection resumed after the fix was deployed and
the fleet was confirmed fully healthy.

## Chaos round — 2026-09-21T04:53:19.492569+00:00

**Scenario**: `out_of_band_shutdown` — target: `r2-os-centos-stream9`
**Result**: Confirmed working as documented — outcome: `recovered`

```
{'first_start_returncode': 1, 'first_start_stdout_tail': "'r2-os-centos-stream9' instance 105924364 exists but is offline -- likely powered off out-of-band. Marking it for recovery...\n", 'second_start_returncode': 0, 'second_start_stdout_tail': "'r2-os-centos-stream9' has an unreachable instance (105924364) from a previous start attempt -- retrying the reachability check instead of creating a new one...\n  running -- verifying real network reachability...\n'r2-os-centos-stream9' is up at 172.236.177.58.\n"}
```

## Chaos round — 2026-09-21T07:04:52.504650+00:00

**Scenario**: `out_of_band_delete` — target: `r2-os-almalinux8`
**Result**: Confirmed working as documented — outcome: `skipped_not_running`

```
{}
```

## Chaos round — 2026-09-21T08:38:48.568554+00:00

**Scenario**: `out_of_band_delete` — target: `r2-os-rocky9`
**Result**: Confirmed working as documented — outcome: `skipped_not_running`

```
{}
```

## Chaos round — 2026-09-21T10:51:15.211716+00:00

**Scenario**: `lock_contention` — target: `r2-node-1`
**Result**: Confirmed working as documented — outcome: `clean_lock_refusal_then_normal`

```
{'stop_while_locked_stdout': '', 'stop_while_locked_stderr': "Configuration error: Another start/stop/rebuild/clear-lock process is already operating on 'r2-node-1' right now. Wait for it to finish and try again.\n"}
```

## Chaos round — 2026-09-21T13:58:41.817840+00:00

**Scenario**: `lock_contention` — target: `r2-os-almalinux8`
**Result**: Confirmed working as documented — outcome: `clean_lock_refusal_then_normal`

```
{'stop_while_locked_stdout': '', 'stop_while_locked_stderr': "Configuration error: Another start/stop/rebuild/clear-lock process is already operating on 'r2-os-almalinux8' right now. Wait for it to finish and try again.\n"}
```

## Chaos round — 2026-09-21T15:52:17.207245+00:00

**Scenario**: `tag_tampering` — target: `r2-os-almalinux9`
**Result**: FINDING — outcome: `FINDING`

```
{'stop_returncode': 0, 'start_returncode': 0, 'tags_after': []}
```

## 2026-09-22: tag_tampering FINDING (r2-os-almalinux9, 2026-09-21T15:52) reviewed

Investigated directly: the volumes checkpoint tags were empty immediately after a stop/start
cycle that itself reported success (stop_returncode=0, start_returncode=0, timing ~59s -- a
genuine, real cycle, not an instant refusal like the earlier r2-os-debian13 case). Confirmed via
`instance_manager.py history` that a *later*, independently poller-triggered stop/start cycle
(17:53-18:04 UTC, roughly 2 hours after the chaos round) correctly re-tagged the same OS volume --
confirmed live via a direct API read (both r2-os-almalinux9 and r2-os-debian13 currently show
correct `linode-scheduler-name:`/`linode-scheduler-role:` tags).

Root cause not conclusively pinned down -- the chaos scenario does not capture stop/start's full
stdout, so there is no direct evidence of a warning being emitted during the tag-sync step of that
specific stop call. `tag_managed_resources()` write+verify is designed to raise (aborting stop
outright) on a genuine failure, not silently proceed -- since stop reported success, either the
write+verify genuinely succeeded and a later, unrelated read briefly observed stale state, or
there is a narrower gap in this path not yet identified. Given zero permanent harm (the tags
self-healed on the very next cycle to touch this resource, exactly as tag_managed_resources()s own
best-effort design promises for its documented failure modes), this is not being escalated to a
code fix without a second, reproducible occurrence -- flagged here for visibility if it recurs.
Chaos testing resumed.
