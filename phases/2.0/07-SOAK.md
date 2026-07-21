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

## Close-out checklist — CLOSED EARLY 2026-07-21 (operator override)

> **The operator elected to cut `v2.0.0` on 2026-07-21**, ~12 days ahead of the
> nominal 2026-08-02 window. The soak's 14 continuous days were NOT completed — the
> gate was consciously overridden after the stack was judged stable (async-plane
> reconnect fix `dc63fda` landed + verified; hero suite green). Recorded, not
> pretended away — see DECISIONS 2026-07-21. Recovery oracle: `python-oracle-final`.

- [~] 14 continuous days — **NOT met** (cut early by operator decision).
- [x] Async plane never wedged — reconnect fix `dc63fda` landed + verified live.
- [x] Hero suite green at close (`make scenarios-hero`).
- [x] **Final-cutover batch:**
  - [x] Canonical `test/lint/fmt/seed/scenarios*` make targets drive Rust
        (`rust-*`); the retired Python `py-*`/`e2e*` targets removed.
  - [x] Archive the Python repo — tag `python-oracle-final` + tarball + pointer
        `docs/PYTHON-ORACLE.md`; Alembic/greenlet retired from the runtime story.
  - [x] `git tag -a v2.0.0` — all-Rust, Alembic retired, soak cut early (see above).
  - [x] **Follow-up done (2026-07-21, post-tag):** ported the demo seed to
        `bss admin seed-demo` (+ `--reset`); `loyalty-wipe`/`demo-restore` not
        carried over (loyalty-cli owns its own DB). See DECISIONS 2026-07-21.

## Notes

- Nothing here blocks development; the soak is a *don't-regress* watch, not a freeze.
- If a regression forces a rebuild + redeploy mid-soak, reset the 14-day clock from
  the redeploy date and note it here.
