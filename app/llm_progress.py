"""Avance de la limpieza LLM: listos, faltan, ritmo y tiempo restante."""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import store
from .freshness import needs_detail_fetch
from .geo import location_incomplete, resolve_city, same_place_ids
from .llm_enrich import LLM_SCHEMA, mark_schema_run, needs_improve, schema_run_started
from .models import Listing


def _listings(city: str | None) -> list[Listing]:
    store.init()
    cid = resolve_city(city) if city else ""
    wanted: list[str] = []
    if cid and cid not in {"fuera", "otros", "argentina", "*"}:
        wanted = [item for item in same_place_ids(cid) if item]
    with store.connect() as conn:
        if wanted:
            marks = ",".join("?" * len(wanted))
            rows = conn.execute(
                f"SELECT * FROM listings WHERE city IN ({marks})",
                tuple(wanted),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM listings").fetchall()
        pins = store._pins_map(conn)
    items: list[Listing] = []
    seen: set[str] = set()
    for row in rows:
        item = store._listing_from_row(row, pins.get(row["id"]))
        if item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)
    return items


def classify(item: Listing) -> str:
    extra = item.extra or {}
    if extra.get("duplicate_of") or extra.get("dedupe_hidden"):
        return "hidden"
    if extra.get("llm_ver") == LLM_SCHEMA and extra.get("llm_ready") and not extra.get("llm_partial"):
        return "done"
    if extra.get("llm_partial") and extra.get("llm_ver") == LLM_SCHEMA:
        return "partial"
    if not needs_improve(item):
        return "done"
    if location_incomplete(item) or extra.get("await_llm"):
        return "no_dir"
    return "rest"


def parse_iso(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    total = int(round(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _sec = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if minutes or not parts:
        parts.append(f"{minutes} min")
    return " ".join(parts)


def pace(
    *,
    listos: int,
    faltan: int,
    started: datetime | None,
    done_at: list[datetime],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    stamps = sorted(t for t in done_at if t <= now)
    start = started
    if stamps:
        start = min(stamps) if start is None else min(start, min(stamps))
    elapsed_sec = (now - start).total_seconds() if start else None
    hour_ago = now.timestamp() - 3600
    n_hour = sum(1 for t in stamps if t.timestamp() >= hour_ago)
    per_hour = 0.0
    if n_hour >= 3:
        per_hour = float(n_hour)
    elif elapsed_sec and elapsed_sec >= 90 and stamps:
        per_hour = len(stamps) / (elapsed_sec / 3600.0)
    per_day = per_hour * 24.0
    eta_sec = (faltan / per_hour * 3600.0) if per_hour > 0 and faltan > 0 else None
    eta_at = ""
    if eta_sec is not None:
        eta_at = datetime.fromtimestamp(now.timestamp() + eta_sec, tz=timezone.utc).astimezone().strftime("%d/%m %H:%M")
    return {
        "lleva_sec": elapsed_sec,
        "lleva": fmt_duration(elapsed_sec),
        "por_hora": round(per_hour, 1),
        "por_dia": round(per_day, 0),
        "faltan_sec": eta_sec,
        "faltan_tiempo": fmt_duration(eta_sec) if eta_sec is not None else "—",
        "eta": eta_at or "—",
        "inicio": start.isoformat() if start else "",
    }


def snapshot(city: str | None = None, *, status_url: str = "") -> dict[str, Any]:
    items = _listings(city)
    counts = {"done": 0, "no_dir": 0, "rest": 0, "partial": 0, "hidden": 0, "need_ficha": 0}
    by_city: dict[str, dict[str, int]] = {}
    done_at: list[datetime] = []
    for item in items:
        kind = classify(item)
        counts[kind] = counts.get(kind, 0) + 1
        extra = item.extra or {}
        if kind in {"done", "partial"} and extra.get("llm_ver") == LLM_SCHEMA:
            stamp = parse_iso(str(extra.get("llm_at") or ""))
            if stamp:
                done_at.append(stamp)
        place = item.city or "?"
        row = by_city.setdefault(
            place, {"avisos": 0, "done": 0, "no_dir": 0, "rest": 0, "partial": 0, "need_ficha": 0}
        )
        row["avisos"] += 1
        if kind != "hidden":
            row[kind] = row.get(kind, 0) + 1
        if kind in {"no_dir", "rest", "partial"} and needs_detail_fetch(item):
            counts["need_ficha"] += 1
            row["need_ficha"] += 1
    visible = counts["done"] + counts["no_dir"] + counts["rest"] + counts["partial"]
    pending = counts["no_dir"] + counts["rest"] + counts["partial"]
    mark_schema_run()
    started = parse_iso(schema_run_started())
    timing = pace(
        listos=counts["done"] + counts["partial"],
        faltan=pending,
        started=started,
        done_at=done_at,
    )
    report = {
        "schema": LLM_SCHEMA,
        "city": resolve_city(city) if city else "",
        "avisos": visible,
        "listos": counts["done"],
        "faltan": pending,
        "sin_direccion": counts["no_dir"],
        "resto": counts["rest"],
        "parciales": counts["partial"],
        "ocultos": counts["hidden"],
        "faltan_ficha": counts["need_ficha"],
        "pct_listos": round(100.0 * counts["done"] / visible, 1) if visible else 0.0,
        "por_ciudad": dict(
            sorted(
                by_city.items(),
                key=lambda kv: (-(kv[1]["no_dir"] + kv[1]["rest"] + kv[1]["partial"]), kv[0]),
            )
        ),
        "cola_llm": {},
        "cola_fichas": {},
        **timing,
    }
    if status_url:
        live = _live_status(status_url)
        if live:
            report["cola_llm"] = live.get("llm") or {}
            report["cola_fichas"] = live.get("details") or {}
    return report


def _live_status(url: str) -> dict[str, Any] | None:
    base = url.rstrip("/")
    req = urllib.request.Request(f"{base}/api/status", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def format_report(data: dict[str, Any]) -> str:
    lines = [
        f"LLM schema {data.get('schema')}",
        f"avisos {data.get('avisos')} · listos {data.get('listos')} ({data.get('pct_listos')}%) · faltan {data.get('faltan')}",
        f"  sin dirección {data.get('sin_direccion')} (prioridad) · resto {data.get('resto')} · parciales {data.get('parciales')}",
        f"  esperan ficha web {data.get('faltan_ficha')}",
        f"lleva {data.get('lleva') or '—'} · {data.get('por_hora') or 0}/h · ~{int(data.get('por_dia') or 0)}/día",
        f"faltan {data.get('faltan_tiempo') or '—'} · ETA {data.get('eta') or '—'}",
    ]
    llm = data.get("cola_llm") or {}
    if llm:
        pending = llm.get("pending")
        urgent = llm.get("urgent")
        rest = llm.get("rest")
        cleaning = llm.get("cleaning")
        workers = llm.get("workers")
        extra = ""
        if urgent is not None:
            extra = f" · urgentes {urgent} · resto cola {rest}"
        provider = llm.get("provider") or ""
        model = llm.get("model") or ""
        who = f" · {provider}/{model}" if provider or model else ""
        lines.append(f"cola en vivo LLM: {pending} pendientes{extra} · limpiando {cleaning}/{workers}{who}")
    details = data.get("cola_fichas") or {}
    if details:
        lines.append(
            f"cola en vivo fichas: {details.get('pending')} pendientes · bajando {details.get('downloading')}"
        )
    city_rows = data.get("por_ciudad") or {}
    if len(city_rows) > 1:
        lines.append("por ciudad (faltan):")
        shown = 0
        for cid, row in city_rows.items():
            left = int(row.get("no_dir") or 0) + int(row.get("rest") or 0) + int(row.get("partial") or 0)
            if left <= 0:
                continue
            lines.append(
                f"  {cid}: faltan {left} (sin dir {row.get('no_dir')}, resto {row.get('rest')}) · listos {row.get('done')}/{row.get('avisos')}"
            )
            shown += 1
            if shown >= 12:
                break
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.llm_progress",
        description="Cuántos avisos ya limpió la LLM, ritmo y tiempo que falta.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  ./scripts/llm-progreso.sh
  ./scripts/llm-progreso.sh --city puerto-madryn
  ./scripts/llm-progreso.sh --watch 10
""",
    )
    parser.add_argument("--city", default="", help="Solo una ciudad, p.ej. puerto-madryn")
    parser.add_argument("--json", action="store_true", help="Salida JSON")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="Base de la API para la cola en vivo",
    )
    parser.add_argument(
        "--watch",
        type=float,
        default=0,
        metavar="SEC",
        help="Repetir cada N segundos",
    )
    parser.add_argument("--no-live", action="store_true", help="No consultar /api/status")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    url = "" if args.no_live else (args.url or "")
    while True:
        data = snapshot(args.city or None, status_url=url)
        if args.json:
            print(json.dumps(data, ensure_ascii=False))
        else:
            print(format_report(data), flush=True)
        if not args.watch or args.watch <= 0:
            return 0
        time.sleep(args.watch)
        if not args.json:
            print("---", flush=True)


if __name__ == "__main__":
    sys.exit(main())
