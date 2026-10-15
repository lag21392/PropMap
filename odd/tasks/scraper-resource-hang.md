# Scraper resource growth and stalled page

## Objective

Prevent scraper backlog and aggregate concurrency from degrading the application over time, and keep an unready listings page from polling continuously without recovery.

## Problem and rationale

The current local changes increase concurrent city/portal work and detail-fetch workers. The detail queue's normal enqueue path can keep appending independently of the refill cap, while the listings UI retries a warming page every 400 ms indefinitely when it has no listings. Together these can amplify load and make the page appear hung.

## Scope and constraints

- Preserve and integrate all pre-existing local modifications; never revert or include unrelated user changes in commits.
- Bound detail-fetch backlog and effective concurrency without blocking scraper ingestion indefinitely; rejected/deferred work must remain eligible for a later retry.
- Give warming-page polling bounded/backoff behavior and a clear recoverable state.
- Do not change external services, production data, credentials, or network configuration.
- Engram task mirror is pending because no Engram tools are available in this session.
- Route: delegated direct, because implementation requires coordinated changes across scraper, UI, and tests.
- TDD: no repository/session TDD setting found; ordinary focused pytest checks are required.

## Tasks

- [x] scraper-budget — Bound detail work and concurrency; add regression coverage for saturation, retryability, worker limits, overflow recovery, and in-flight cleanup.
- [x] warming-recovery — Prevent rapid unbounded warming-page requests and preserve a clear way to resume loading; validate frontend syntax.

## Acceptance criteria

- Detail work cannot grow without a configured bound, and hitting the bound does not permanently lose listings needing detail fetch.
- Simultaneous scrape jobs cannot multiply detail-fetch threads beyond the process-level budget.
- A warming listings page does not issue a rapid request loop forever; users retain a recoverable path to refresh.
- Existing scraping, queue retry, and listings behavior remains covered by focused tests.

## Verification

- TDD source: no configured TDD mode found; runner: pytest.
- Host focused runner: `PYTHONPATH=/mnt/md0/REPOS_HDD/PropMap-prod pytest tests/test_detail_fetch.py`
- Frontend syntax: `node --check static/app.js`
- Whitespace: `git diff --check -- app/detail_fetch.py static/app.js tests/test_detail_fetch.py odd/tasks/scraper-resource-hang.md`
- Runtime boundary: production is Docker-based; `compose.prod.yaml` bind-mounts `app` and `static` read-only. Validation was host-side only. No nonproduction test container was identified, so the production service was not run or restarted.

## Progress

- Investigation confirmed unbounded normal detail enqueueing, per-portal worker pools that multiply across concurrent city jobs, and indefinite 400 ms retries for an empty warming result.
- Pre-existing worktree changes overlap the implementation; user authorized preserving and integrating them.
- The detail queue now caps pending IDs at `MAX_PENDING`, shares the configured `DETAIL_WORKERS` budget across queue and scrape fetches, resumes bounded DB backfill as queue capacity frees, and always clears an in-flight ID after missing-record returns or exceptions unless it was requeued.
- The card-page warming response now propagates into the same bounded exponential-backoff/recovery path as the listings response; after eight empty warming retries polling pauses and the user can manually retry without losing already loaded cards.
- Verification observed on host Python 3.10.12 / pytest 8.4.1: focused detail tests — 21 passed; queue recovery/cleanup selection — 3 passed; `node --check static/app.js` — exit 0; `git diff --check` for the changed source, tests, and task document — exit 0.
- Runtime limitation: no frontend browser harness or safely identified nonproduction Docker test target was available. Container behavior remains unverified; production Docker services were not run or restarted. Nothing was staged or committed because implementation changes share files with pre-existing local edits, and staging whole files would include unrelated user work.
