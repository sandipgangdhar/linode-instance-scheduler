# long-running-test-log.md

## Kickoff — soak test infrastructure validated, starting now

## Long-running soak — checkpoint 2026-09-18T05:01:02.762338+00:00

Poller running continuously (interval 120s, window 300s) against a
real 12-instance fleet with realistic multi-cycle daily schedules (dev-fleet-a: 4x2h
windows/day UTC, dev-fleet-b: 3x1.5h windows/day IST, r2-node-1: 3x1h individual-schedule
windows/day). 1 rotating check cycles completed since the last checkpoint, 0
finding(s) recorded (see the JSONL event log for full detail on any).
