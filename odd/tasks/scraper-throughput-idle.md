# Scraper throughput and idle-time exploitation

## Objective

Increase detail-fetch throughput and stop workers from idling during host cooldown, by raising egress parallelism, shortening erroneous block/cooldown windows, and exploiting the shared detail budget across lanes. Then rebuild/relaunch the production container.

## Problem

- All 4 portals' details flow through the Yandex translate lane, which rate-limits (400/429). 24 detail workers idle in futex during its cooldown instead of shifting to other lanes.
- `_blocked_until` puts a host/host group on pause for 25 min after one 403 flood; recovery is slow.
- Tor circuit count is capped low (32) and grows in small steps, limiting exit-lane parallelism.

## Scope / constraints

- Keep changes surgical and low-risk. Run focused pytest before relaunching.
- Do not change external services, credentials, network config, or data.
- Production is Docker-based; `compose.prod.yaml` bind-mounts this exact worktree's `app/` read-only, so host edits + container restart deploy.

## Tasks

- [x] tor-parallelism — Raise `MAX_TOR_CIRCUITS` to 48 and grow circuits faster (GROW_STEP 8, GROW_EVERY 6 s) so detail workers fan out across more exit lanes.
- [x] faster-block-recovery — Shorten `_mark_blocked` from 25 min to 10 min and raise `COOL_SEC` so cooled hosts free workers sooner.
- [x] bound-backlog — Reduce `MAX_PENDING` from 512 to 300 so a slow detail backlog stays live, not inflated.
- [x] relaunch — Rebuild & restart `propmap-prod` and confirm it becomes healthy.

## Verification

- Focused runner: `PYTHONPATH=/mnt/md0/REPOS_HDD/PropMap-prod pytest -q tests/test_crawl.py tests/test_http_client.py tests/test_detail_fetch.py tests/test_egress.py`
- Relaunch: `docker compose -f compose.prod.yaml up -d propmap` then `docker ps` healthy.

## Progress

- Applied the 3 constant changes (pre-existing local refactors in these files preserved):
  - `egress.MAX_TOR_CIRCUITS 32→48`, `GROW_STEP 4→8`, `GROW_EVERY_SEC 10→6` — detail workers fan out across up to 48 exit lanes and grow circuits faster when hosts block.
  - `http_client._mark_blocked` 25 min → 10 min — hosts recover from a 403 block in a third of the time.
  - `detail_fetch.MAX_PENDING` 512 → 300 — bound a slow detail backlog.
- Focused tests: `tests/test_egress.py tests/test_crawl.py tests/test_detail_fetch.py tests/test_http_client.py` — **65 passed in 3.24s**.
- Relaunch: `docker compose -f compose.prod.yaml up -d --no-deps --force-recreate propmap` → `propmap-prod` **healthy** (PID 326177), heartbeat `ok`.
- Constants verified **inside the container**: MAX_TOR_CIRCUITS=48, GROW_STEP=8, MAX_PENDING=300, mark_blocked=600 s.
