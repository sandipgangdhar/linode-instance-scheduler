# long-running-test-log.md

## Kickoff — soak test infrastructure validated, starting now

## Long-running soak — checkpoint 2026-09-18T05:01:02.762338+00:00

Poller running continuously (interval 120s, window 300s) against a
real 12-instance fleet with realistic multi-cycle daily schedules (dev-fleet-a: 4x2h
windows/day UTC, dev-fleet-b: 3x1.5h windows/day IST, r2-node-1: 3x1h individual-schedule
windows/day). 1 rotating check cycles completed since the last checkpoint, 0
finding(s) recorded (see the JSONL event log for full detail on any).

## Real kickoff — 2026-09-18T05:02:56Z

Both harnesses relaunched clean after the coordination fix above (see chaos-testing-log.md's
own correction entry). Now running unattended against the real 12-instance fleet
(dev-fleet-a: 6 members, 4x2h daily windows, UTC; dev-fleet-b: 4 members, 3x1.5h daily
windows, Asia/Kolkata; r2-node-1: individual schedule, 3x1h daily windows, UTC, overriding
its own group membership -- the standing override-precedence proof). Target: 1-2 weeks of
continuous unattended operation, checked in on periodically, with every finding (soak or
chaos) recorded here and in chaos-testing-log.md as it happens. Goal: reach a point where a
production deployment recommendation is backed by sustained, not just point-in-time, live
evidence.

## Long-running soak — checkpoint 2026-09-18T05:07:56.351703+00:00

Poller running continuously (interval 120s, window 300s) against a
real 12-instance fleet with realistic multi-cycle daily schedules (dev-fleet-a: 4x2h
windows/day UTC, dev-fleet-b: 3x1.5h windows/day IST, r2-node-1: 3x1h individual-schedule
windows/day). 0 rotating check cycles completed since the last checkpoint, 0
finding(s) recorded (see the JSONL event log for full detail on any).

## Long-running soak — checkpoint 2026-09-18T07:00:13.872945+00:00

Poller running continuously (interval 120s, window 300s) against a
real 12-instance fleet with realistic multi-cycle daily schedules (dev-fleet-a: 4x2h
windows/day UTC, dev-fleet-b: 3x1.5h windows/day IST, r2-node-1: 3x1h individual-schedule
windows/day). 0 rotating check cycles completed since the last checkpoint, 0
finding(s) recorded (see the JSONL event log for full detail on any).

## Infrastructure migration + enhanced disaster-recovery coverage -- 2026-09-18T06:55Z

Both harnesses moved off the developer's own laptop onto a dedicated Linode instance
(`soak-chaos-runner`, in-bom-2, tagged `internal-infra`/`soak-chaos-runner`), running as real
`systemd` services (`Restart=always`, enabled for boot) instead of a detached background
process -- confirmed to survive a genuine reboot (triggered one for real, both services came
back `active` automatically). This closes the gap where the run depended on the laptop staying
powered on. No test-fleet impact: cutover was sequenced so the Mac-side poller was fully
stopped and confirmed dead before the runner's own poller started, so there was never a moment
with two independent pollers racing the same schedule.

Also, at request, the existing daily disaster-recovery drill (already fully deleting
`state/instances.db` and reconstructing it via a real `rebuild` against Linode's own tags -- a
genuine total-database-loss scenario, not a partial corruption) now verifies the FULL metadata
surface comes back: every individual instance's schedule and every schedule group's own
definition, not just instance identity. Group membership is compared by group name rather than
its internal numeric id, since a recreated group legitimately gets a new id on rebuild -- that
would otherwise have been a guaranteed false alarm on every single drill run.

## Long-running soak — checkpoint 2026-09-18T13:00:43.332944+00:00

Poller running continuously (interval 120s, window 300s) against a
real 12-instance fleet with realistic multi-cycle daily schedules (dev-fleet-a: 4x2h
windows/day UTC, dev-fleet-b: 3x1.5h windows/day IST, r2-node-1: 3x1h individual-schedule
windows/day). 12 rotating check cycles completed since the last checkpoint, 0
finding(s) recorded (see the JSONL event log for full detail on any).

## Round 2 finding -- 2026-09-18

While the soak test was running against the round-2 fleet, a routine check-in caught several
`dev-fleet-b` group members being reported as `fired_failure` for an ordinary scheduled
"create" that opened while they were already running (from an earlier, overlapping window) --
every instance was actually healthy the whole time. Root cause: `poll_tick()`'s success/failure
classification treated "already running" / "already stopped" (documented no-op outcomes,
nothing wrong) the same as a real failure, which meant `poll --once`'s exit code -- the intended
signal for a cron/systemd wrapper or monitoring script to detect a tick that needs attention --
could report failure on a run where nothing was actually wrong. Fixed with a new, distinct
no-op outcome so a genuine no-op is never counted as a failure. Shipped as `v1.2.3`.

Cutting that release also surfaced two problems in the publish pipeline itself, both fixed the
same day: the automated GitHub Actions publish workflow had never actually succeeded on a
genuinely clean checkout (a missing dependency-install step for part of the export process,
undetected by local testing since that machine already had the dependency installed); and the
release script's wipe-and-replace step briefly deleted this log and its sibling
`chaos-testing-log.md` before restoring both from git history and fixing the script so every
file meant to persist across a release is protected the same way, not just the first one that
needed it.

Both fleets restarted cleanly on the fixed code with zero disruption.

## Long-running soak — checkpoint 2026-09-18T14:18:22.177981+00:00

Poller running continuously (interval 120s, window 300s) against a
real 12-instance fleet with realistic multi-cycle daily schedules (dev-fleet-a: 4x2h
windows/day UTC, dev-fleet-b: 3x1.5h windows/day IST, r2-node-1: 3x1h individual-schedule
windows/day). 0 rotating check cycles completed since the last checkpoint, 0
finding(s) recorded (see the JSONL event log for full detail on any).

## Long-running soak — checkpoint 2026-09-18T20:18:35.196846+00:00

Poller running continuously (interval 120s, window 300s) against a
real 12-instance fleet with realistic multi-cycle daily schedules (dev-fleet-a: 4x2h
windows/day UTC, dev-fleet-b: 3x1.5h windows/day IST, r2-node-1: 3x1h individual-schedule
windows/day). 12 rotating check cycles completed since the last checkpoint, 0
finding(s) recorded (see the JSONL event log for full detail on any).

## Long-running soak — checkpoint 2026-09-19T02:18:38.814087+00:00

Poller running continuously (interval 120s, window 300s) against a
real 12-instance fleet with realistic multi-cycle daily schedules (dev-fleet-a: 4x2h
windows/day UTC, dev-fleet-b: 3x1.5h windows/day IST, r2-node-1: 3x1h individual-schedule
windows/day). 12 rotating check cycles completed since the last checkpoint, 0
finding(s) recorded (see the JSONL event log for full detail on any).

## Long-running soak — checkpoint 2026-09-19T08:18:55.664844+00:00

Poller running continuously (interval 120s, window 300s) against a
real 12-instance fleet with realistic multi-cycle daily schedules (dev-fleet-a: 4x2h
windows/day UTC, dev-fleet-b: 3x1.5h windows/day IST, r2-node-1: 3x1h individual-schedule
windows/day). 12 rotating check cycles completed since the last checkpoint, 0
finding(s) recorded (see the JSONL event log for full detail on any).

## Long-running soak — checkpoint 2026-09-19T14:19:07.200174+00:00

Poller running continuously (interval 120s, window 300s) against a
real 12-instance fleet with realistic multi-cycle daily schedules (dev-fleet-a: 4x2h
windows/day UTC, dev-fleet-b: 3x1.5h windows/day IST, r2-node-1: 3x1h individual-schedule
windows/day). 12 rotating check cycles completed since the last checkpoint, 0
finding(s) recorded (see the JSONL event log for full detail on any).
