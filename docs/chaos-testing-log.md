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
