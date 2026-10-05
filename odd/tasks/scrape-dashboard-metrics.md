# Scrape dashboard throughput metrics

## Objective

Make the scrape dashboard's progress metrics represent the work actually performed and label stock, new-item movement, and rolling-window data accurately.

## Problem and rationale

The dashboard's “Fichas” pace is built from detail-fetch telemetry, but the inline detail path in the active city scrape does not record outcomes. Also, the top summary can substitute rolling 24-hour movements when today's counters are zero while still labeling them “hoy.” The dashboard therefore can under-report current activity and mislabel the time window.

## Scope and constraints

- Preserve all existing local modifications; don't revert, stage, or commit unrelated user changes.
- Count inline detail attempts using the same success/failure semantics as background detail workers.
- Clearly distinguish total stock, newly discovered ads, fetched detail records, and the actual time window for rates/movement.
- Do not run, restart, or modify production Docker services.
- Engram mirror pending: no Engram tools are available in this session.
- Route: delegated direct; change spans metric producers, dashboard display, and regression tests.
- TDD: no project/session TDD configuration found; use focused existing pytest runner.

## Tasks

- [x] inline-detail-telemetry — Record inline detail success/failure and verify shared pace accounting.
- [x] dashboard-metric-labels — Distinguish scrape ingestion progress from newly found listings and correctly label calendar-day vs rolling-24-hour movements.

## Acceptance criteria

- Inline and background detail work both contribute to detail throughput and failure metrics.
- Dashboard provides an actionable measure of scrape-ingested records, separately from newly discovered records.
- Top summary labels rolling 24-hour fallback as such, not as “hoy.”
- Focused metric tests and JavaScript syntax checks pass; no production runtime is changed.

## Verification

- TDD source: no configured TDD mode found; runner: pytest.
- Focused runner: `pytest -q tests/test_ops.py tests/test_detail_fetch.py`
- Frontend syntax: `node --check static/tablero.js`
- Whitespace: `git diff --check` for changed paths.
- Runtime harness: N/A; production is Docker-based and no nonproduction browser/container harness has been identified.

## Progress

- Mapping confirmed inline detail telemetry omission and ambiguous “hoy” label on rolling-window fallback.
- Existing worktree already contains unrelated changes in the relevant files; preserve them and make targeted edits only.
- Inline detail attempts now emit one `details` outcome after a needed fetch: `ok` only when `details_scraped` is true, otherwise `fail`; already-detailed listings remain uncounted. Added producer-level success/failure/skip coverage.
- The process table now separates scrape intake (all accepted listings, including existing ads) from newly discovered ads and detail fetches. It shows current-minute and last-hour intake, rate/hour, and calendar-day / rolling-24-hour totals. In-memory rolling totals are labeled as process-scoped unless the full window is covered.
- Summary movement fallback now selects one consistent time window and says “últimas 24 h” only when that complete window is available; otherwise it reports the current process scope.
- Validation observed:
  - `PYTHONPATH=/mnt/md0/REPOS_HDD/PropMap-prod pytest -q --tb=short tests/test_ops.py tests/test_detail_fetch.py` — **38 passed in 61.93s** (final rerun).
  - `node --check static/tablero.js` — **passed** (exit 0).
  - `git diff --check -- app/pipeline.py app/ops.py static/tablero.js tests/test_ops.py` — **passed** (exit 0).
- No production scrape, Docker command, or runtime service was run or modified. The Engram mirror remains pending because Engram tools are unavailable in this session.
