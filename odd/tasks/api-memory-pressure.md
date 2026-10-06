# API memory pressure

## Objective

Reduce sustained and peak RAM use in the PropMap API under scraper and cache load without losing listings or disabling normal cache behavior.

## Problem and evidence

The production Uvicorn process was observed at about 20 GiB RSS, almost entirely private anonymous memory, with roughly 4.7 GiB swapped. Its RSS remained near 20 GiB across short samples. The scraper can materialize complete result and catalog lists concurrently, while the RAM watchdog evicts listing dictionaries but leaves card-row references in separate cache structures. This supports memory-pressure risks but does not yet prove a single cause or a leak.

## Scope and constraints

- Fix memory retained by the listing/card cache and reduce redundant full-catalog materialization during scrapes.
- Preserve user-visible listings, ordering, persistence, and scraper retry behavior.
- Preserve all pre-existing worktree changes; do not touch runtime data or restart production.
- Route: delegated direct; coordinated changes require code, regression tests, and this task record.
- TDD: no explicit project/session mode found; use focused pytest.
- Delivery: `feature-branch-chain`, selected pragmatically for the existing feature branch after no answer was available; no PR was created.
- Engram mirror: pending because no Engram tools are available in this session.

## Tasks

- [x] cache-eviction — RAM-pressure eviction clears listing rows, card rows, secondary card-cache entries, and uncompressed pin blobs when a gzip copy exists; it also drops the uncompressed listing blob whenever a gzip copy exists, while keeping gzip-serving behavior if disk is unavailable.
- [x] scraper-materialization — Scraper result references are released before fetching the persisted catalog for detail enrichment; that fetch now filters by active portal in SQL, avoiding a full-city read per portal worker while preserving query ordering and existing unfiltered callers.
- [x] regression-checks — Added disk-recovery, card-cache eviction (including gzip-backed pin eviction), in-flight card-build invalidation, scraper-reference-release, and SQL source-filter/result-order coverage.

## Acceptance criteria

- Under RAM pressure, snapshots and secondary card caches do not continue retaining the discarded per-listing objects.
- Cache reads can recover the same listing/card data from the existing disk-backed source after eviction.
- Scraper processing avoids unnecessary simultaneous full-catalog copies and preserves result semantics.
- Focused tests pass; no production service or data is changed.

## Verification

- Focused runner: `pytest -q tests/test_listings_cache.py tests/test_daily_cycle.py tests/test_store_pool.py`
- Whitespace check: `git diff --check`

## Progress

- Baseline inspection found pre-existing user changes in `data/heartbeat` and untracked `data/laya_cache/`; neither is part of this task.
- Production memory measurements are historical only; the local API endpoint is unavailable in this session, so no live behavior will be changed or inferred from a restart.
- Focused verification: `PYTHONPATH=. pytest -q tests/test_listings_cache.py tests/test_daily_cycle.py tests/test_store_pool.py` — 68 passed in 2.90s.
- Follow-up verification: `PYTHONPATH=. pytest -q tests/test_listings_cache.py tests/test_daily_cycle.py tests/test_store_pool.py` — 68 passed in 2.94s; `PYTHONPATH=. pytest -q tests/test_store_extra.py` — 5 passed in 0.44s.
- Pressure-eviction follow-up: `PYTHONPATH=. pytest -q tests/test_listings_cache.py tests/test_daily_cycle.py tests/test_store_pool.py tests/test_store_extra.py` — 74 passed in 3.21s.
- Parent spot-check on the final worktree: same focused suite — 74 passed in 2.91s.
- `git diff --check` passed.
- Native receipt review preflight returned `immutable_review_transport_unsupported` for this runtime; final native risk assessment was medium with `review_due=false` (`under_budget`). No review approval or deployment is claimed.
- Engram mirror remains pending because no Engram tools are available in this session.
