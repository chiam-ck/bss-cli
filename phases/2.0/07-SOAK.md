# 07 — Phase 8 soak (the final gate before `v2.0.0`)

The last Phase 8 exit criterion (`03-PHASES.md` §Phase 8): *"14-day soak on the
all-Rust stack before calling it done."* This file is the soak's start marker + the
checklist to close it. It is the gate for the final `v2.0.0` tag.

## Start

- **Soak start:** 2026-07-19 (Phase 8 items 1–5 shipped; see `PROGRESS.md`).
- **Target close:** **2026-08-02** (14 days), assuming no regression resets the clock.
- **Stack under soak:** the all-Rust `docker-compose.rust.yml` overlay — 9 service
  images + 2 portals, all `:rust`, against the shared `tech-vm` Postgres / RabbitMQ /
  Jaeger. 11/11 `GET /health` → 200 at start.
- **Baseline to hold:** the motto-#6 numbers in [`06-MOTTO6-REMEASURE.md`](06-MOTTO6-REMEASURE.md)
  (app-plane RAM ~82 MiB idle / ~160 MiB warmed, cold-start ~3 s, p99 well under
  50 ms) and the hero suite (19/19).

## What "passing" means

The soak passes if, across 14 continuous days on the all-Rust stack:

1. **No unexplained restarts / crashes.** Containers stay up; any restart is
   root-caused, not shrugged off.
2. **No memory creep.** App-plane RSS stays in the ~80–180 MiB band (idle ~82,
   warmed ~160). Absolute number isn't the concern — a *slow unbounded climb* is a
   leak → investigate, don't wait it out.
3. **Async plane stays alive.** The MQ relay + safe consumers keep draining — the
   known **`bss-events` MqChannel no-reconnect** failure mode (a broker blip wedging
   the async plane until restart, see memory `rust-mq-relay-no-reconnect` /
   `ca8b572`) is the #1 thing to watch. A wedge that needs a manual restart is a
   soak **fail** until the reconnect fix lands.
4. **Hero suite still 19/19** on a spot re-run mid-soak and at close.
5. **No new error-log signatures** beyond the known-benign set.

## How to check in (lightweight, daily-ish)

```bash
# health of all 11
for p in 8001 8002 8003 8004 8005 8006 8007 8008 8010 9001 9002; do \
  printf '%s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:$p/health; done

# app-plane RAM (watch for creep vs 06-MOTTO6-REMEASURE.md)
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep bss-cli-

# restarts since start (RestartCount should stay 0)
docker ps --filter label=com.docker.compose.project=bss-cli \
  --format '{{.Names}}' | while read n; do \
    printf '%s %s\n' "$n" "$(docker inspect -f '{{.RestartCount}}' "$n")"; done

# async plane: any parked/wedged queues? (MQ relay liveness)
#   check RabbitMQ mgmt / queue depths on tech-vm; a stuck monotonic depth = a wedge.
```

## Close-out checklist (run at 2026-08-02, gate for `v2.0.0`)

- [ ] 14 continuous days, no clock-resetting regression.
- [ ] RestartCount 0 across all 11 (or every restart root-caused + benign).
- [ ] RAM still in band (no leak).
- [ ] Async plane never wedged (or the reconnect fix landed + re-soaked).
- [ ] Hero suite 19/19 at close.
- [ ] Then the **final-cutover batch** (deliberately held until now so the Python
      oracle stayed runnable through the soak):
  - [ ] Rename the canonical `test/lint/fmt/seed/scenarios*/e2e` make targets from
        the Python oracle to the Rust ones (`rust-*` become canonical).
  - [ ] Full runbook pass — the remaining Python-ism commands (`v1.2-pipeline-deploy`
        alembic downgrade, `phase-execution` dev loop, `snapshot-regeneration` +
        `three-provider-sandbox-soak` pytest, `cockpit` python introspection) that
        were left intertwined with the archive.
  - [ ] Archive the Python repo with a pointer README; retire greenlet/alembic from
        the runtime story.
  - [ ] `git tag -a v2.0.0` — all-Rust, Alembic retired, soak passed.

## Notes

- Nothing here blocks development; the soak is a *don't-regress* watch, not a freeze.
- If a regression forces a rebuild + redeploy mid-soak, reset the 14-day clock from
  the redeploy date and note it here.
