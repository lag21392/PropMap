"""Telemetría del scrape: carriles, HTTP, fichas, LLM y stock en SQLite.

En memoria para no pelear el candado del scraper. El inventario se lee
aparte, cacheado. Grafana no hace falta: /tablero consume esto. /metrics
queda por si más adelante se engancha un scraper de Prometheus.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import crawl

KEEP_MIN = 24 * 60
SERIES_MIN = KEEP_MIN
_INV_TTL = 8.0
DASH_TTL = 1.5
_started = time.time()
_lock = threading.Lock()
_totals: dict[tuple, int] = {}
_minutes: dict[int, dict[tuple, int]] = {}
_last_prune = 0
_inv: dict[str, Any] | None = None
_inv_at = 0.0
_pace_hold: dict[str, dict[str, Any]] = {}
_dash: dict[str, Any] | None = None
_dash_at = 0.0
_dash_inflight = False
_dash_lock = threading.Lock()


def reset() -> None:
    global _inv, _inv_at, _last_prune, _dash, _dash_at, _dash_inflight
    with _lock:
        _totals.clear()
        _minutes.clear()
        _last_prune = 0
    _inv = None
    _inv_at = 0.0
    _pace_hold.clear()
    with _dash_lock:
        _dash = None
        _dash_at = 0.0
        _dash_inflight = False


def note(metric: str, n: int = 1, **labels: Any) -> None:
    name = (metric or "").strip()[:40]
    if not name or n <= 0:
        return
    tags = tuple(sorted((str(k)[:24], str(v)[:60]) for k, v in labels.items() if v is not None and v != ""))
    key = (name, tags)
    minute = int(time.time() // 60)
    with _lock:
        _totals[key] = _totals.get(key, 0) + int(n)
        bucket = _minutes.get(minute)
        if bucket is None:
            bucket = {}
            _minutes[minute] = bucket
        bucket[key] = bucket.get(key, 0) + int(n)
        global _last_prune
        if minute != _last_prune:
            _last_prune = minute
            cutoff = minute - KEEP_MIN
            for old in [slot for slot in _minutes if slot < cutoff]:
                _minutes.pop(old, None)
    if name in {"new", "gone", "ingest"}:
        _persist(name, int(n), minute)
    elif name in {"llm", "details"}:
        outcome = str(labels.get("outcome") or "ok")[:24]
        _persist(f"{name}.{outcome}", int(n), minute)


def snapshot() -> dict[str, Any]:
    now_min = int(time.time() // 60)
    with _lock:
        totals = dict(_totals)
        minutes = {slot: dict(bucket) for slot, bucket in _minutes.items()}
    http = _roll(totals, "http")
    groups: dict[str, int] = defaultdict(int)
    for lane, n in (http.get("by_lane") or {}).items():
        groups[_lane_group(lane)] += n
    if groups:
        http["by_group"] = dict(groups)
    llm = _roll(totals, "llm", dim="outcome")
    details = _roll(totals, "details", dim="outcome")
    ingest = _roll(totals, "ingest", dim="source")
    fresh = _roll(totals, "new", dim="source")
    gone = _roll(totals, "gone")
    disk = _disk_window(now_min - SERIES_MIN + 1, now_min)
    series = []
    for slot in range(now_min - SERIES_MIN + 1, now_min + 1):
        bucket = minutes.get(slot) or {}
        saved = disk.get(slot) or {}
        llm_by = _merge_outcome(saved, _by_label(bucket, "llm", "outcome"), "llm")
        details_by = _merge_outcome(saved, _by_label(bucket, "details", "outcome"), "details")
        row = {
            "t": slot * 60,
            "http": _sum_metric(bucket, "http"),
            "llm": _disk_or_mem(saved, _sum_metric(bucket, "llm"), "llm.ok", "llm.partial"),
            "details": _disk_or_mem(saved, _sum_metric(bucket, "details"), "details.ok", "details.partial"),
            "ingest": int(saved.get("ingest") or 0) or _sum_metric(bucket, "ingest"),
            "new": int(saved.get("new") or 0) or _sum_metric(bucket, "new"),
            "gone": int(saved.get("gone") or 0) or _sum_metric(bucket, "gone"),
            "lanes": _lane_groups(bucket),
            "llm_by": llm_by,
            "details_by": details_by,
        }
        series.append(row)
    return {
        "since": datetime.fromtimestamp(_started, timezone.utc).isoformat(),
        "uptime_s": int(time.time() - _started),
        "http": http,
        "llm": llm,
        "details": details,
        "ingest": ingest,
        "new": fresh,
        "gone": gone,
        "series": series,
    }


def inventory(force: bool = False) -> dict[str, Any]:
    global _inv, _inv_at
    now = time.time()
    if not force and _inv is not None and now - _inv_at < _INV_TTL:
        return _inv
    from .llm_enrich import LLM_SCHEMA
    from .store import connect

    labels = _city_labels()
    try:
        with connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] or 0)
            detailed = int(
                conn.execute("SELECT COUNT(*) FROM listings WHERE details_scraped = 1").fetchone()[0] or 0
            )
            llm_row = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN llm_ready = 1 AND llm_ver = ? AND llm_partial = 0 THEN 1 ELSE 0 END),
                  SUM(llm_partial),
                  SUM(llm_await),
                  SUM(needs_llm),
                  SUM(CASE WHEN details_scraped = 0 AND is_hidden = 0 THEN 1 ELSE 0 END)
                FROM listings
                """,
                (LLM_SCHEMA,),
            ).fetchone()
            sources = [
                {"id": row[0] or "?", "n": int(row[1])}
                for row in conn.execute(
                    "SELECT source, COUNT(*) FROM listings GROUP BY source ORDER BY COUNT(*) DESC"
                )
            ]
            cities = []
            for row in conn.execute(
                "SELECT city, COUNT(*) FROM listings GROUP BY city ORDER BY COUNT(*) DESC LIMIT 24"
            ):
                cid = row[0] or "?"
                label = labels.get(cid) or {"fuera": "Sin ciudad", "otros": "Otros", "argentina": "Argentina"}.get(
                    cid, cid.replace("-", " ")
                )
                cities.append({"id": cid, "label": label, "n": int(row[1])})
    except Exception:
        if _inv is not None:
            return _inv
        return {
            "listings": 0,
            "details": 0,
            "details_need": 0,
            "details_pct": 0.0,
            "llm_done": 0,
            "llm_partial": 0,
            "await_llm": 0,
            "llm_need": 0,
            "llm_pct": 0.0,
            "sources": [],
            "cities": [],
            "schema": LLM_SCHEMA,
        }
    llm_done = int(llm_row[0] or 0)
    llm_partial = int(llm_row[1] or 0)
    await_llm = int(llm_row[2] or 0)
    llm_need = int(llm_row[3] or 0)
    details_need = int(llm_row[4] or 0)
    data = {
        "listings": total,
        "details": detailed,
        "details_need": details_need,
        "details_pct": round(100.0 * detailed / total, 1) if total else 0.0,
        "llm_done": llm_done,
        "llm_partial": llm_partial,
        "await_llm": await_llm,
        "llm_need": llm_need,
        "llm_pct": round(100.0 * llm_done / total, 1) if total else 0.0,
        "sources": sources,
        "cities": cities,
        "schema": LLM_SCHEMA,
    }
    _inv = data
    _inv_at = now
    return data


def dashboard() -> dict[str, Any]:
    """Respuesta rápida: cache caliente y armado en otro hilo."""
    now = time.time()
    with _dash_lock:
        cached = _dash
        age = now - _dash_at if cached is not None else 1e9
        busy = _dash_inflight
    if cached is not None and age < DASH_TTL:
        return cached
    if cached is not None:
        if not busy:
            _kick_dash()
        return cached
    return _store_dash(_build_dashboard())


def _kick_dash() -> None:
    global _dash_inflight
    with _dash_lock:
        if _dash_inflight:
            return
        _dash_inflight = True
    threading.Thread(target=_dash_job, daemon=True, name="ops-dash").start()


def _dash_job() -> None:
    global _dash_inflight
    try:
        _store_dash(_build_dashboard())
    except Exception:
        pass
    finally:
        with _dash_lock:
            _dash_inflight = False


def _store_dash(data: dict[str, Any]) -> dict[str, Any]:
    global _dash, _dash_at
    with _dash_lock:
        _dash = data
        _dash_at = time.time()
    return data


def _build_dashboard() -> dict[str, Any]:
    from .egress import snapshot as egress_snapshot
    from .pipeline import status

    live = status()
    jobs = {}
    for city_id, job in (live.get("jobs") or {}).items():
        if not (job.get("running") or job.get("error") or job.get("paused")):
            continue
        jobs[city_id] = {
            "running": bool(job.get("running")),
            "paused": bool(job.get("paused")),
            "mode": job.get("mode") or "",
            "message": job.get("message") or "",
            "error": job.get("error") or "",
            "counts": job.get("counts") or {},
            "eta_min": job.get("eta_min"),
        }
    tel = snapshot()
    inv = inventory()
    llm_live = live.get("llm") or {}
    details_live = live.get("details") or {}
    copy_live = live.get("copy") or {}
    llm_pace = _outcome_pace(tel.get("series") or [], "llm_by", prefix="llm")
    details_pace = _outcome_pace(tel.get("series") or [], "details_by", prefix="details")
    llm_rate = float(llm_pace.get("per_hour") or 0)
    details_rate = float(details_pace.get("per_hour") or 0)
    llm_need_n = int(inv.get("llm_need") or 0)
    details_need_n = int(inv.get("details_need") or 0)
    llm_pace["eta_h"] = round(llm_need_n / llm_rate, 1) if llm_rate and llm_need_n else None
    details_pace["eta_h"] = round(details_need_n / details_rate, 1) if details_rate and details_need_n else None
    new_n, gone_n, seen_n = _movement_totals(tel)
    new_24h, gone_24h, seen_24h = _movement_totals(tel, since_min=int(time.time() // 60) - KEEP_MIN + 1)
    pass_new = int((live.get("counts") or {}).get("avisos") or 0)
    visits: list[dict[str, Any]] = []
    try:
        from .matomo import last_visits

        visits = last_visits(12)
    except Exception:
        visits = []
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live": {
            "running": bool(live.get("running")),
            "running_cities": list(live.get("running_cities") or []),
            "mode": live.get("mode") or "",
            "message": live.get("message") or "",
            "paused": bool(live.get("paused")),
            "eta_min": live.get("eta_min") or 0,
            "queue": list(live.get("queue") or []),
            "counts": live.get("counts") or {},
            "llm": llm_live,
            "details": details_live,
            "copy": live.get("copy") or {},
            "crawl": live.get("crawl") or {},
            "logs": list(live.get("logs") or [])[-24:],
            "jobs": jobs,
            "last_run": live.get("last_run") or "",
        },
        "egress": egress_snapshot(),
        "busy": crawl.busy_rows(),
        "telemetry": tel,
        "inventory": inv,
        "movement": {
            "new": new_n,
            "gone": gone_n,
            "net": new_n - gone_n,
            "seen": seen_n,
            "pass_new": pass_new,
            "new_24h": new_24h,
            "gone_24h": gone_24h,
            "seen_24h": seen_24h,
            "scope": "today" if _persist_on() else "process",
        },
        "llm_pipe": {
            "done": int(inv.get("llm_done") or 0),
            "need": int(inv.get("llm_need") or 0),
            "partial": int(inv.get("llm_partial") or 0),
            "await_dir": int(inv.get("await_llm") or 0),
            "queue": int(llm_live.get("pending") or 0),
            "ready": int(llm_live.get("ready") or 0),
            "saving": int(llm_live.get("saving") or 0),
            "working": int(llm_live.get("cleaning") or 0),
            "workers": int(llm_live.get("workers") or 0),
            "cap": int(llm_live.get("cap") or 0),
            "gpu": bool(llm_live.get("gpu")) or bool(copy_live.get("cleaning")),
            "llama_ok": bool(llm_live.get("llama_ok")),
            "n_ctx": int(llm_live.get("n_ctx") or 0),
            "busy_s": float(llm_live.get("busy_s") or 0) or float(copy_live.get("busy_s") or 0),
            "busy": list(llm_live.get("busy") or []),
            "copy_queue": int(copy_live.get("pending") or 0),
            "copy_working": int(copy_live.get("cleaning") or 0),
            "copy_cap": int(copy_live.get("cap") or 0),
            "pct": float(inv.get("llm_pct") or 0),
            **llm_pace,
        },
        "details_pipe": {
            "done": int(inv.get("details") or 0),
            "need": int(inv.get("details_need") or 0),
            "queue": int(details_live.get("pending") or 0),
            "cooling": int(details_live.get("cooling") or 0),
            "working": int(details_live.get("downloading") or 0),
            "workers": int(details_live.get("workers") or 0),
            "pct": float(inv.get("details_pct") or 0),
            **details_pace,
        },
        "visits": visits,
    }


def prometheus() -> str:
    tel = snapshot()
    inv = inventory()
    egress = {}
    try:
        from .egress import snapshot as egress_snapshot

        egress = egress_snapshot()
    except Exception:
        egress = {}
    lines = [
        "# HELP propmap_up 1 si el proceso responde.",
        "# TYPE propmap_up gauge",
        "propmap_up 1",
        "# HELP propmap_uptime_seconds Segundos desde el arranque del proceso.",
        "# TYPE propmap_uptime_seconds gauge",
        f"propmap_uptime_seconds {tel['uptime_s']}",
        "# HELP propmap_listings Avisos en SQLite.",
        "# TYPE propmap_listings gauge",
        f"propmap_listings {inv['listings']}",
        "# HELP propmap_listings_detailed Avisos con ficha bajada.",
        "# TYPE propmap_listings_detailed gauge",
        f"propmap_listings_detailed {inv['details']}",
        "# HELP propmap_listings_llm Avisos con LLM listo (schema actual).",
        "# TYPE propmap_listings_llm gauge",
        f"propmap_listings_llm {inv['llm_done']}",
        "# HELP propmap_egress_lanes Carriles de salida configurados.",
        "# TYPE propmap_egress_lanes gauge",
        f"propmap_egress_lanes {int(egress.get('lanes') or 0)}",
    ]
    for row in inv.get("sources") or []:
        sid = _prom_label(row.get("id") or "unknown")
        lines.append(f'propmap_listings_source{{source="{sid}"}} {int(row["n"])}')
    http = tel.get("http") or {}
    for lane, n in (http.get("by_lane") or {}).items():
        lines.append(f'propmap_http_total{{lane="{_prom_label(lane)}"}} {int(n)}')
    for status, n in (http.get("by_status") or {}).items():
        lines.append(f'propmap_http_status_total{{status="{_prom_label(status)}"}} {int(n)}')
    for host, n in (http.get("by_host") or {}).items():
        lines.append(f'propmap_http_host_total{{host="{_prom_label(host)}"}} {int(n)}')
    for outcome, n in ((tel.get("llm") or {}).get("by_outcome") or {}).items():
        lines.append(f'propmap_llm_total{{outcome="{_prom_label(outcome)}"}} {int(n)}')
    for outcome, n in ((tel.get("details") or {}).get("by_outcome") or {}).items():
        lines.append(f'propmap_details_total{{outcome="{_prom_label(outcome)}"}} {int(n)}')
    for source, n in ((tel.get("ingest") or {}).get("by_source") or {}).items():
        lines.append(f'propmap_ingest_total{{source="{_prom_label(source)}"}} {int(n)}')
    for group, n in (http.get("by_group") or {}).items():
        lines.append(f'propmap_http_group_total{{group="{_prom_label(group)}"}} {int(n)}')
    lines.extend(
        [
            "# HELP propmap_listings_new_total Avisos nuevos vistos desde el arranque.",
            "# TYPE propmap_listings_new_total counter",
            f"propmap_listings_new_total {int((tel.get('new') or {}).get('n') or 0)}",
            "# HELP propmap_listings_gone_total Avisos dados de baja desde el arranque.",
            "# TYPE propmap_listings_gone_total counter",
            f"propmap_listings_gone_total {int((tel.get('gone') or {}).get('n') or 0)}",
            "# HELP propmap_listings_llm_need Avisos de la base que todavía no pasaron el LLM.",
            "# TYPE propmap_listings_llm_need gauge",
            f"propmap_listings_llm_need {int(inv.get('llm_need') or 0)}",
        ]
    )
    live_llm = {}
    live_details = {}
    try:
        from .llm_enrich import queue_stats
        from .detail_fetch import queue_stats as detail_stats

        live_llm = queue_stats()
        live_details = detail_stats()
    except Exception:
        pass
    lines.append(f'propmap_queue{{kind="llm"}} {int(live_llm.get("pending") or 0)}')
    lines.append(f'propmap_queue{{kind="details"}} {int(live_details.get("pending") or 0)}')
    return "\n".join(lines) + "\n"


def _disk_or_mem(saved: dict[str, int], mem_n: int, *keys: str) -> int:
    disk_n = sum(int(saved.get(key) or 0) for key in keys)
    return disk_n or int(mem_n or 0)


def _merge_outcome(saved: dict[str, int], mem: dict[str, int], prefix: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for outcome in ("ok", "partial", "fail", "retry"):
        n = int(saved.get(f"{prefix}.{outcome}") or 0) or int((mem or {}).get(outcome) or 0)
        if n:
            out[outcome] = n
    return out


def _day_outcomes(prefix: str) -> tuple[int, int, int]:
    if not _persist_on():
        return 0, 0, 0
    try:
        from .store import ops_sum_since

        start = _today_start_min()
        return (
            ops_sum_since(f"{prefix}.ok", start),
            ops_sum_since(f"{prefix}.partial", start),
            ops_sum_since(f"{prefix}.fail", start),
        )
    except Exception:
        return 0, 0, 0


def _local_day_hours() -> float:
    now_min = int(time.time() // 60)
    return max(0.25, (now_min - _today_start_min()) / 60.0)


def _outcome_pace(series: list[dict[str, Any]], field: str, *, prefix: str = "") -> dict[str, Any]:
    hour = series[-60:] if series else []
    now = series[-1] if series else {}
    by_now = now.get(field) or {}

    def grab(rows: list[dict[str, Any]], outcome: str) -> int:
        return sum(int((row.get(field) or {}).get(outcome) or 0) for row in rows)

    ok_h = grab(hour, "ok")
    partial_h = grab(hour, "partial")
    fail_h = grab(hour, "fail")
    ok_24 = grab(series, "ok")
    partial_24 = grab(series, "partial")
    fail_24 = grab(series, "fail")
    this_ok = int(by_now.get("ok") or 0)
    this_partial = int(by_now.get("partial") or 0)
    this_fail = int(by_now.get("fail") or 0)
    this_min = this_ok + this_partial
    last_hour = ok_h + partial_h
    day_ok, day_partial, day_fail = _day_outcomes(prefix) if prefix else (0, 0, 0)
    if not (day_ok or day_partial) and not _persist_on():
        day_ok, day_partial, day_fail = ok_h, partial_h, fail_h
    day_done = day_ok + day_partial
    if last_hour > 0:
        per_hour = float(last_hour)
        rate_scope = "hour"
    elif day_done > 0:
        per_hour = float(day_done) / _local_day_hours()
        rate_scope = "day"
    else:
        per_hour = 0.0
        rate_scope = "none"
    result = {
        "this_min": this_min,
        "this_ok": this_ok,
        "this_fail": this_fail,
        "ok_h": ok_h,
        "partial_h": partial_h,
        "fail_h": fail_h,
        "ok_d": day_ok,
        "partial_d": day_partial,
        "fail_d": day_fail,
        "ok_24": ok_24,
        "partial_24": partial_24,
        "fail_24": fail_24,
        "per_hour": round(per_hour, 1),
        "per_min": round(per_hour / 60.0, 2) if per_hour else 0.0,
        "per_day": round(float(day_done), 1),
        "rate_scope": rate_scope,
    }
    if per_hour <= 0:
        held = _pace_hold.get(field)
        if held and float(held.get("per_hour") or 0) > 0:
            result["per_hour"] = held["per_hour"]
            result["per_min"] = round(float(held["per_hour"]) / 60.0, 2)
            result["rate_scope"] = "hold"
            result["held"] = True
    else:
        _pace_hold[field] = {"per_hour": result["per_hour"], "rate_scope": rate_scope}
    return result


def _city_labels() -> dict[str, str]:
    try:
        from .listings_cache import public_meta

        rows = public_meta().get("cities") or []
    except Exception:
        rows = []
    out: dict[str, str] = {}
    for row in rows:
        cid = str((row or {}).get("id") or "").strip()
        label = str((row or {}).get("label") or "").strip()
        if cid and label:
            out[cid] = label
    return out


def _roll(totals: dict[tuple, int], metric: str, dim: str | None = None) -> dict[str, Any]:
    n = 0
    by_lane: dict[str, int] = defaultdict(int)
    by_status: dict[str, int] = defaultdict(int)
    by_host: dict[str, int] = defaultdict(int)
    by_dim: dict[str, int] = defaultdict(int)
    for (name, tags), value in totals.items():
        if name != metric:
            continue
        n += value
        mapped = dict(tags)
        if mapped.get("lane"):
            by_lane[mapped["lane"]] += value
        if mapped.get("status"):
            by_status[mapped["status"]] += value
        if mapped.get("host"):
            by_host[mapped["host"]] += value
        if dim and mapped.get(dim):
            by_dim[mapped[dim]] += value
    out: dict[str, Any] = {"n": n}
    if by_lane:
        out["by_lane"] = dict(by_lane)
    if by_status:
        out["by_status"] = dict(by_status)
    if by_host:
        out["by_host"] = dict(sorted(by_host.items(), key=lambda item: item[1], reverse=True)[:12])
    if dim:
        out[f"by_{dim}"] = dict(by_dim)
    return out


def _sum_metric(bucket: dict[tuple, int], metric: str) -> int:
    return sum(n for (name, _tags), n in bucket.items() if name == metric)


def _by_label(bucket: dict[tuple, int], metric: str, label: str) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for (name, tags), n in bucket.items():
        if name != metric:
            continue
        mapped = dict(tags)
        key = mapped.get(label)
        if key:
            out[key] += n
    return dict(out)


def _lane_group(lane: str) -> str:
    token = (lane or "").lower()
    if token in {"direct", "urllib"}:
        return "local"
    if token == "tor" or token.startswith("tor-"):
        return "tor"
    if token.startswith("proxy") or token == "vpn":
        return "vpn"
    if token == "translate":
        return "translate"
    if token == "stealth":
        return "stealth"
    return "otros"


def _lane_groups(bucket: dict[tuple, int]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for (name, tags), n in bucket.items():
        if name != "http":
            continue
        mapped = dict(tags)
        out[_lane_group(mapped.get("lane") or "direct")] += n
    return dict(out)


def _persist_on() -> bool:
    if os.environ.get("PROPMAP_OPS_PERSIST") == "1":
        return True
    return os.environ.get("PROPMAP_TEST") != "1"


def _persist(metric: str, n: int, minute: int) -> None:
    if not _persist_on():
        return
    try:
        from .store import ops_bump

        ops_bump(metric, n, minute)
    except Exception:
        return


def _disk_window(start_min: int, end_min: int) -> dict[int, dict[str, int]]:
    if not _persist_on():
        return {}
    try:
        from .store import ops_window

        return ops_window(start_min, end_min)
    except Exception:
        return {}


def _today_start_min() -> int:
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))
    except Exception:
        now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() // 60)


def _movement_totals(tel: dict[str, Any], since_min: int | None = None) -> tuple[int, int, int]:
    process_new = int((tel.get("new") or {}).get("n") or 0)
    process_gone = int((tel.get("gone") or {}).get("n") or 0)
    process_seen = int((tel.get("ingest") or {}).get("n") or 0)
    if not _persist_on():
        return process_new, process_gone, process_seen
    try:
        from .store import ops_sum_since

        start = int(since_min) if since_min is not None else _today_start_min()
        return (
            ops_sum_since("new", start),
            ops_sum_since("gone", start),
            ops_sum_since("ingest", start),
        )
    except Exception:
        return process_new, process_gone, process_seen


def _prom_label(value: Any) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value)[:60])
