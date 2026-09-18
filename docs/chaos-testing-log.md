# chaos-testing-log.md

## Kickoff — chaos monkey infrastructure validated, starting now

## Chaos round — 2026-09-18T04:59:26.016732+00:00

**Scenario**: `lock_contention` — target: `r2-os-ubuntu2004`
**Result**: FINDING — outcome: `FINDING`

```
{'stop_while_locked_stdout': '', 'stop_while_locked_stderr': "Configuration error: Another start/stop/rebuild/clear-lock process is already operating on 'r2-os-ubuntu2004' right now. Wait for it to finish and try again.\n"}
```
