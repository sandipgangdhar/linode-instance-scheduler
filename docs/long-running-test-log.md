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
