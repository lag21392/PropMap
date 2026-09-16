from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import SOURCE_LABELS

MAX_FAVORITES = 10
PAGE_W = 595.28
PAGE_H = 841.89
MARGIN = 48.0
LEAD = 12.5
TITLE_SIZE = 13
BODY_SIZE = 9.5
WRAP = 92

_TYPE = {
    "casa": "Casa",
    "departamento": "Departamento",
    "ph": "PH",
    "terreno": "Terreno",
    "local": "Local",
    "oficina": "Oficina",
    "galpon": "Galpón",
}
_DASH = str.maketrans(
    {
        "–": "-",
        "—": "-",
        "•": "-",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "…": "...",
        "\u00a0": " ",
        "\u202f": " ",
    }
)


def build_report(rows: list[dict[str, Any]], username: str = "", total: int = 0) -> bytes:
    picked = list(rows or [])[:MAX_FAVORITES]
    when = datetime.now().strftime("%d/%m/%Y")
    who = username or "cuenta"
    head = f"PropMap  ·  Favoritos de {who}  ·  {when}"
    note = ""
    if int(total or 0) > len(picked):
        note = f"Mostramos {len(picked)} de {int(total)} favoritos (tope {MAX_FAVORITES})."
    elif not picked:
        note = "No hay favoritos para listar."
    pages: list[list[tuple[str, str]]] = []
    buf: list[tuple[str, str]] = []

    def flush() -> None:
        if buf:
            pages.append(list(buf))
            buf.clear()

    if note:
        buf.append(("muted", note))
        buf.append(("gap", ""))
    if not picked:
        buf.append(("title", "Reporte de favoritos"))
        buf.append(("muted", note or "Sin avisos."))
    for idx, row in enumerate(picked, start=1):
        block = _listing_lines(idx, row)
        if buf and len(buf) + len(block) >= 50:
            flush()
        if buf:
            buf.append(("gap", ""))
        for line in block:
            if len(buf) >= 50:
                flush()
            buf.append(line)
    flush()
    return _pdf(head, pages)


def _listing_lines(idx: int, row: dict[str, Any]) -> list[tuple[str, str]]:
    kind = _TYPE.get(str(row.get("property_type") or ""), str(row.get("property_type") or "Aviso"))
    title = str(row.get("title") or "Sin título")
    lines: list[tuple[str, str]] = [
        ("title", f"{idx}. {kind} · {title}"),
        ("body", _money(row)),
    ]
    for label, value in _facts(row):
        if value in (None, "", [], "—"):
            continue
        text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        for wrapped in _wrap(f"{label}: {text}", WRAP):
            lines.append(("body", wrapped))
    notes = str(row.get("notes") or "").strip()
    if notes:
        lines.append(("body", "Notas:"))
        for wrapped in _wrap(notes, WRAP):
            lines.append(("body", wrapped))
    desc = str(row.get("description") or "").strip()
    if desc:
        lines.append(("body", "Descripción:"))
        for wrapped in _wrap(desc[:1800], WRAP):
            lines.append(("body", wrapped))
    return lines


def _punto_mapa(row: dict[str, Any]) -> str:
    if row.get("location_missing"):
        return "Sin ubicación"
    if row.get("location_real") or (row.get("has_exact_location") and not row.get("location_approx")):
        if str(row.get("location_kind") or "") == "intersection":
            return "Ubicación real (intersección)"
        return "Ubicación real (calle y altura)"
    if row.get("intersection") and not row.get("street_number"):
        return "Intersección aproximada"
    if row.get("portal_lat") is not None or row.get("approx_address") or row.get("location_approx"):
        return "Aproximada (manzana del aviso)"
    if not (row.get("address") or row.get("intersection") or row.get("lat")):
        return "Sin ubicación"
    return "Aproximada"


def _facts(row: dict[str, Any]) -> list[tuple[str, Any]]:
    sources = row.get("sources") or []
    source_txt = ""
    if isinstance(sources, list) and sources:
        parts = []
        for src in sources:
            if not isinstance(src, dict):
                continue
            label = src.get("label") or SOURCE_LABELS.get(str(src.get("source") or ""), src.get("source"))
            url = src.get("url") or ""
            parts.append(f"{label} {url}".strip())
        source_txt = " | ".join(parts)
    loc = _punto_mapa(row)
    return [
        ("Link", row.get("url") or ""),
        ("Fuentes", source_txt),
        ("Id", row.get("id") or ""),
        ("Dirección", row.get("address") or ""),
        ("Intersección", row.get("intersection") or ""),
        ("Entre calles", row.get("between") or ""),
        ("Dirección aprox.", row.get("approx_address") or ""),
        ("Punto en el mapa", loc),
        ("Barrio", row.get("barrio") or ""),
        ("Zona", row.get("zona") or ""),
        ("Ciudad", str(row.get("city") or "").replace("-", " ")),
        ("Cubiertos", _m2(row.get("covered_m2"))),
        ("Terreno / lote", _m2(row.get("lot_m2") or row.get("total_m2"))),
        ("Ambientes", row.get("rooms")),
        ("Dormitorios", row.get("bedrooms")),
        ("Baños", row.get("bathrooms")),
        ("Cochera", "Sí" if row.get("parking") else ""),
        ("Antigüedad", f"{row.get('age_years')} años" if row.get("age_years") is not None else ""),
        ("Expensas", _num(row.get("expenses"), "USD ")),
        ("USD/m²", _num(row.get("price_m2"), "USD ")),
        ("Piso", row.get("floor")),
        ("Orientación", row.get("orientation") or ""),
        ("Estado", row.get("condition") or ""),
        ("Apto crédito", "Sí" if row.get("mortgage_credit") is True else "No dice" if row.get("mortgage_credit") is False else ""),
        ("Inmobiliaria", row.get("publisher") or ""),
        ("Publicado", row.get("published_at") or ""),
        ("Ganga", row.get("deal_label") or ""),
        ("Score ganga", row.get("deal_score")),
        ("Calidad", _quality(row)),
        ("Renta mensual est.", _num(row.get("monthly_rent_usd"), "USD ")),
        ("Renta temporal est.", _num(row.get("nightly_usd"), "USD/noche ")),
        ("Contactado", "Sí" if row.get("contacted") else "No"),
        ("Amenities", row.get("amenities") or row.get("tags") or []),
        ("Fotos", row.get("photos") or ([row.get("image")] if row.get("image") else [])),
    ]


def _money(row: dict[str, Any]) -> str:
    price = row.get("price_usd") if row.get("price_usd") not in (None, "") else row.get("price")
    if price in (None, ""):
        return "Precio no publicado"
    try:
        n = float(price)
    except (TypeError, ValueError):
        return f"Precio {price}"
    cur = "USD" if row.get("price_usd") not in (None, "") else (row.get("currency") or "USD")
    return f"Precio {cur} {n:,.0f}".replace(",", ".")


def _m2(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.0f} m2"
    except (TypeError, ValueError):
        return str(value)


def _num(value: Any, prefix: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{prefix}{float(value):.0f}"
    except (TypeError, ValueError):
        return f"{prefix}{value}"


def _quality(row: dict[str, Any]) -> str:
    score = row.get("quality_score")
    label = row.get("quality_label") or ""
    if score in (None, "") and not label:
        return ""
    if score in (None, ""):
        return label
    try:
        return f"{float(score):.0f} · {label}".strip(" ·")
    except (TypeError, ValueError):
        return f"{score} {label}".strip()


def _latin(text: str) -> str:
    return str(text or "").translate(_DASH).encode("latin-1", "replace").decode("latin-1")


def _escape(text: str) -> str:
    return _latin(text).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, width: int) -> list[str]:
    raw = _latin(text).replace("\r", " ").replace("\n", " ").strip()
    if not raw:
        return []
    words = raw.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        chunks = [word[i : i + width] for i in range(0, len(word), width)] or [word]
        for chunk in chunks:
            trial = f"{cur} {chunk}".strip() if cur else chunk
            if len(trial) <= width:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = chunk
    if cur:
        lines.append(cur)
    return lines


def _pdf(header: str, pages: list[list[tuple[str, str]]]) -> bytes:
    if not pages:
        pages = [[("muted", "Sin avisos.")]]
    streams = [_page_stream(header, page, i + 1, len(pages)) for i, page in enumerate(pages)]
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    font_reg = 3
    font_bold = 4
    page_ids = [5 + i * 2 for i in range(len(streams))]
    content_ids = [pid + 1 for pid in page_ids]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    for page_id, content_id, stream in zip(page_ids, content_ids, streams):
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
                f"/Contents {content_id} 0 R /Resources << /Font << /F1 {font_reg} 0 R /F2 {font_bold} 0 R >> >> >>"
            ).encode("ascii")
        )
        payload = stream if stream.endswith(b"\n") else stream + b"\n"
        objects.append(b"<< /Length %d >>\nstream\n" % len(payload) + payload + b"endstream")

    parts = [b"%PDF-1.4\n"]
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(sum(len(p) for p in parts))
        parts.append(f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref_at = sum(len(p) for p in parts)
    xref = [b"xref\n", f"0 {len(objects) + 1}\n".encode("ascii"), b"0000000000 65535 f \n"]
    for off in offsets[1:]:
        xref.append(f"{off:010d} 00000 n \n".encode("ascii"))
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return b"".join(parts + xref + [trailer])


def _page_stream(header: str, lines: list[tuple[str, str]], page: int, total: int) -> bytes:
    y = PAGE_H - MARGIN
    ops = ["BT"]
    ops.append(f"/F1 8 Tf 1 0 0 1 {MARGIN:.2f} {PAGE_H - 28:.2f} Tm ({_escape(header)}) Tj")
    ops.append(
        f"/F1 8 Tf 1 0 0 1 {PAGE_W - MARGIN - 70:.2f} {PAGE_H - 28:.2f} Tm ({_escape(f'p. {page}/{total}')}) Tj"
    )
    for kind, text in lines:
        if kind == "gap":
            y -= LEAD * 0.7
            continue
        if y < MARGIN + 20:
            break
        if kind == "title":
            ops.append(f"/F2 {TITLE_SIZE} Tf 1 0 0 1 {MARGIN:.2f} {y:.2f} Tm ({_escape(text)}) Tj")
            y -= LEAD + 2
        elif kind == "muted":
            ops.append(f"/F1 8 Tf 1 0 0 1 {MARGIN:.2f} {y:.2f} Tm ({_escape(text)}) Tj")
            y -= LEAD
        else:
            ops.append(f"/F1 {BODY_SIZE} Tf 1 0 0 1 {MARGIN:.2f} {y:.2f} Tm ({_escape(text)}) Tj")
            y -= LEAD
    ops.append("ET")
    return "\n".join(ops).encode("latin-1", "replace")
