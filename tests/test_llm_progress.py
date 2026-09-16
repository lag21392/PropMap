from datetime import datetime, timezone

from app.llm_enrich import LLM_SCHEMA
from app.llm_progress import classify, format_report, fmt_duration, pace
from app.models import Listing


def _item(source_id: str, **kwargs) -> Listing:
    data = dict(
        source="zonaprop",
        source_id=source_id,
        url="https://example.com/" + source_id,
        title="Depto",
        property_type="departamento",
        city="puerto-madryn",
    )
    data.update(kwargs)
    return Listing(**data)


def test_classify_done_no_dir_and_rest():
    done = _item("ok", address="Mitre 100", extra={"llm_ver": LLM_SCHEMA, "llm_ready": True})
    lost = _item("lost", address="Puerto Madryn", extra={"llm_ver": 5, "llm_ready": True})
    rest = _item("rest", address="Roca 2400", extra={"llm_ver": 5, "llm_ready": True})
    hidden = _item("dup", extra={"duplicate_of": "zonaprop:1"})
    assert classify(done) == "done"
    assert classify(lost) == "no_dir"
    assert classify(rest) == "rest"
    assert classify(hidden) == "hidden"


def test_format_report_mentions_priority_and_counts():
    text = format_report(
        {
            "schema": 6,
            "avisos": 100,
            "listos": 10,
            "pct_listos": 10.0,
            "faltan": 90,
            "sin_direccion": 12,
            "resto": 78,
            "parciales": 0,
            "faltan_ficha": 5,
            "lleva": "2 h 10 min",
            "por_hora": 12.0,
            "por_dia": 288,
            "faltan_tiempo": "7 h 30 min",
            "eta": "28/08 06:00",
            "cola_llm": {"pending": 40, "urgent": 8, "rest": 32, "cleaning": 1, "workers": 1, "provider": "gemini", "model": "gemini-3.5-flash-lite"},
            "cola_fichas": {"pending": 3, "downloading": 1},
            "por_ciudad": {},
        }
    )
    assert "faltan 90" in text
    assert "sin dirección 12" in text
    assert "urgentes 8" in text
    assert "gemini/gemini-3.5-flash-lite" in text
    assert "lleva 2 h 10 min" in text
    assert "12.0/h" in text
    assert "~288/día" in text
    assert "faltan 7 h 30 min" in text


def test_pace_uses_last_hour_and_eta():
    now = datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)
    started = datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc)
    done_at = [
        datetime(2026, 8, 28, 2, 10, tzinfo=timezone.utc),
        datetime(2026, 8, 28, 2, 20, tzinfo=timezone.utc),
        datetime(2026, 8, 28, 2, 40, tzinfo=timezone.utc),
        datetime(2026, 8, 28, 2, 50, tzinfo=timezone.utc),
    ]
    stats = pace(listos=4, faltan=8, started=started, done_at=done_at, now=now)
    assert stats["por_hora"] == 4.0
    assert stats["por_dia"] == 96.0
    assert stats["lleva"] == "2 h"
    assert stats["faltan_tiempo"] == "2 h"
    assert fmt_duration(120) == "2 min"
