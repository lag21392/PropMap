from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import Any

# Flujo: elkjs (motor Sugiyama que usa Xyflow).
# DER: Mermaid erDiagram a partir del SQLite real (PRAGMA + FKs inferidas).
# Módulos: imports internos de app/ parseados con ast.

APP_DIR = Path(__file__).resolve().parent
_SKIP_TABLES = {"sqlite_sequence", "sqlite_stat1", "sqlite_stat4"}
_TABLE_ALIASES = {
    "user": "accounts",
    "listing": "listings",
    "session": "account_sessions",
}
_TABLE_NOTES = {
    "listings": "Avisos de venta. Fuente de verdad del mapa.",
    "pins": "Favoritos y notas del visitante o de la cuenta.",
    "price_history": "Precio de un aviso a lo largo del tiempo.",
    "market_snapshots": "Medianas diarias por ciudad y tipo.",
    "rental_comps": "Alquileres para estimar yield.",
    "visits": "Eventos propios (además de Matomo).",
    "meta": "Claves sueltas: sitio Matomo, flags.",
    "accounts": "Usuarios. Email y nombre van cifrados.",
    "account_sessions": "Sesiones firmadas.",
    "account_vaults": "Pines de la cuenta, cifrados.",
}


_er_memo: tuple[str, dict[str, Any]] | None = None
_mod_memo: dict[str, Any] | None = None


def blueprint() -> dict[str, Any]:
    flow = graph()
    return {
        "engine": "elkjs+mermaid",
        "flow": flow,
        "er": entity_diagram(),
        "modules": module_graph(),
        "nodes": flow["nodes"],
        "edges": flow["edges"],
        "algorithm": flow["algorithm"],
        "direction": flow["direction"],
    }


def graph() -> dict[str, Any]:
    nodes = [
        _node("zonaprop", "ZonaProp", "portal", "Listados y fichas del portal."),
        _node("mercadolibre", "Mercado Libre", "portal", "Listados y fichas del portal."),
        _node("argenprop", "Argenprop", "portal", "Listados y fichas del portal."),
        _node("properati", "Properati", "portal", "Listados y fichas del portal."),
        _node("scrapers", "Scrapers", "extract", "app/scrapers: HTML → Listing."),
        _node("details", "Fichas", "extract", "Detalle, fotos y texto largo."),
        _node("geo", "Geo / pin", "transform", "Ciudad, barrio, radio y tags de lugar."),
        _node("places", "Lugares API", "transform", "Georef y Nominatim. Sin listas fijas."),
        _node("llm", "LLM", "transform", "Enriquece tags, crédito y notas."),
        _node("store", "SQLite", "store", "data/listings.sqlite. Fuente de verdad."),
        _node("snaps", "Snaps / cache", "store", "JSON por ciudad para el mapa."),
        _node("api", "/api/listings", "serve", "Lo que consume el navegador."),
        _node("ui", "Mapa y lista", "serve", "static/app.js. Filtros y pentágono."),
        _node("matomo", "Matomo", "observe", "Visitas vía /q/l (y /matomo.php). Tablero en /stats."),
        _node("market", "Mercado / renta", "observe", "Medianas, gangas y yields."),
        _node("pois", "POIs / acceso", "observe", "Overpass: servicios cerca del pin."),
    ]
    edges = [
        _edge("zonaprop", "scrapers"),
        _edge("mercadolibre", "scrapers"),
        _edge("argenprop", "scrapers"),
        _edge("properati", "scrapers"),
        _edge("scrapers", "details"),
        _edge("details", "geo"),
        _edge("places", "geo"),
        _edge("geo", "store"),
        _edge("llm", "store"),
        _edge("store", "snaps"),
        _edge("store", "market"),
        _edge("store", "pois"),
        _edge("snaps", "api"),
        _edge("api", "ui"),
        _edge("ui", "matomo"),
        _edge("pois", "ui"),
        _edge("market", "ui"),
    ]
    return {
        "engine": "elkjs",
        "algorithm": "layered",
        "direction": "RIGHT",
        "nodes": nodes,
        "edges": edges,
    }


def entity_diagram() -> dict[str, Any]:
    global _er_memo
    from . import store

    store.init()
    with store.connect() as conn:
        fingerprint = _schema_fingerprint(conn)
        if _er_memo and _er_memo[0] == fingerprint:
            return _er_memo[1]
        data = _build_er(conn)
    _er_memo = (fingerprint, data)
    return data


def module_graph() -> dict[str, Any]:
    global _mod_memo
    if _mod_memo is None:
        _mod_memo = _build_modules()
    return _mod_memo


def _sql_ident(name: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise ValueError(f"nombre de tabla inválido: {name}")
    return '"' + name + '"'


def _schema_fingerprint(conn: Any) -> str:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return hashlib.sha256(repr([(str(r[0]), str(r[1] or "")) for r in rows]).encode()).hexdigest()


def _build_er(conn: Any) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    seen_edges: set[str] = set()
    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        if str(row[0]) not in _SKIP_TABLES
    ]
    table_set = set(names)
    declared: dict[str, list[tuple[str, str]]] = {}
    for name in names:
        ident = _sql_ident(name)
        fks = conn.execute(f"PRAGMA foreign_key_list({ident})").fetchall()
        declared[name] = [(str(row[2]), str(row[3])) for row in fks]
        cols = []
        for row in conn.execute(f"PRAGMA table_info({ident})").fetchall():
            cols.append(
                {
                    "name": str(row[1]),
                    "type": str(row[2] or "TEXT"),
                    "pk": bool(row[5]),
                }
            )
        rows = int(conn.execute(f"SELECT COUNT(*) FROM {ident}").fetchone()[0])
        note = _TABLE_NOTES.get(name, f"Tabla {name}.")
        tables.append(
            {
                "id": name,
                "label": name,
                "kind": "store",
                "note": f"{note} {rows} filas.",
                "rows": rows,
                "columns": cols,
            }
        )
    for table in tables:
        name = table["id"]
        col_names = {col["name"] for col in table["columns"]}
        for dest, src_col in declared.get(name, []):
            if dest in table_set:
                _add_rel(edges, seen_edges, name, dest, src_col, "declared")
                _mark_fk(table["columns"], src_col)
        for col in list(col_names):
            dest = _guess_table(col, table_set, name)
            if dest:
                _add_rel(edges, seen_edges, name, dest, col, "inferred")
                _mark_fk(table["columns"], col)
    return {
        "engine": "mermaid",
        "kind": "erDiagram",
        "mermaid": _mermaid_er(tables, edges),
        "tables": tables,
        "nodes": tables,
        "edges": edges,
    }


def _cluster_mod(name: str) -> str:
    if name.startswith("scrapers."):
        return "scrapers"
    if name.startswith("llm_"):
        return "llm"
    return name


def _build_modules() -> dict[str, Any]:
    files = [
        path
        for path in APP_DIR.rglob("*.py")
        if path.name != "__init__.py" and "__pycache__" not in path.parts
    ]
    orig = {_mod_name(path) for path in files}
    members: dict[str, list[str]] = {}
    for name in orig:
        members.setdefault(_cluster_mod(name), []).append(name)
    edges: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in files:
        src = _cluster_mod(_mod_name(path))
        for dest in _internal_imports(path, orig):
            _add_rel(edges, seen, src, _cluster_mod(dest), "import", "import")
    used = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
    nodes = []
    for name in sorted(used):
        kids = members.get(name, [name])
        extra = f" Agrupa {len(kids)} archivos." if len(kids) > 1 else ""
        nodes.append(_node(name, name, _mod_kind(name), f"app/{name.replace('.', '/')}.py{extra}"))
    return {
        "engine": "elkjs",
        "algorithm": "layered",
        "direction": "RIGHT",
        "nodes": nodes,
        "edges": edges,
    }


def _guess_table(column: str, tables: set[str], owner: str) -> str | None:
    if not column.endswith("_id"):
        return None
    stem = column[:-3]
    for candidate in (stem, stem + "s", stem + "es", _TABLE_ALIASES.get(stem, "")):
        if candidate and candidate in tables and candidate != owner:
            return candidate
    return None


def _mark_fk(columns: list[dict[str, Any]], name: str) -> None:
    for col in columns:
        if col["name"] == name:
            col["fk"] = True


def _add_rel(
    edges: list[dict[str, str]],
    seen: set[str],
    src: str,
    dst: str,
    label: str,
    kind: str,
) -> None:
    key = f"{src}->{dst}:{label}"
    if key in seen or src == dst:
        return
    seen.add(key)
    edges.append({"id": key, "source": src, "target": dst, "label": label, "kind": kind})


_MERMAID_RESERVED = {
    "end",
    "title",
    "meta",
    "direction",
    "class",
    "classdef",
    "style",
    "click",
    "comment",
    "graph",
    "subgraph",
}


def _er_ident(name: str, prefix: str) -> str:
    raw = name or "field"
    if raw.lower() in _MERMAID_RESERVED or not re.match(r"^[A-Za-z_][\w-]*$", raw):
        return prefix + re.sub(r"[^A-Za-z0-9_]", "_", raw)
    return raw


def _mermaid_er(tables: list[dict[str, Any]], edges: list[dict[str, str]]) -> str:
    lines = ["erDiagram"]
    for rel in edges:
        lines.append(
            f'  {_er_ident(rel["target"], "t_")} ||--o{{ {_er_ident(rel["source"], "t_")} : {rel["label"]}'
        )
    for table in tables:
        lines.append(f'  {_er_ident(table["id"], "t_")} {{')
        for col in table["columns"]:
            marks = []
            if col.get("pk"):
                marks.append("PK")
            if col.get("fk"):
                marks.append("FK")
            suffix = " " + ", ".join(marks) if marks else ""
            safe_type = (col["type"] or "string").replace(" ", "_")
            attr = _er_ident(col["name"], "f_")
            comment = f' "{col["name"]}"' if attr != col["name"] else ""
            lines.append(f"    {safe_type} {attr}{suffix}{comment}")
        lines.append("  }")
    return "\n".join(lines) + "\n"


def _mod_name(path: Path) -> str:
    rel = path.relative_to(APP_DIR).with_suffix("")
    return ".".join(rel.parts)


def _mod_kind(name: str) -> str:
    if name.startswith("scrapers") or name == "scrapers":
        return "extract"
    if name == "llm":
        return "transform"
    if name in {"store", "listings_cache", "jsoncodec"}:
        return "store"
    if name in {"main", "matomo_gate", "search_auth", "accounts"}:
        return "serve"
    if name in {"matomo", "analytics", "watchdog"}:
        return "observe"
    if name in {"geo", "places", "place_api", "place_tags", "llm_enrich", "market"}:
        return "transform"
    return "serve"


def _internal_imports(path: Path, names: set[str]) -> set[str]:
    found: set[str] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return found
    pkg = _package_of(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = _from_base(pkg, node)
            if base is None:
                continue
            if base in names:
                found.add(base)
            for alias in node.names:
                for cand in ((f"{base}.{alias.name}" if base else alias.name), alias.name):
                    if cand in names:
                        found.add(cand)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    cand = alias.name[4:]
                    if cand in names:
                        found.add(cand)
    return found


def _from_base(pkg: str, node: ast.ImportFrom) -> str | None:
    module = node.module or ""
    if node.level == 0:
        if module == "app":
            return ""
        if module.startswith("app."):
            return module[4:]
        return None
    parts = pkg.split(".") if pkg else []
    up = node.level - 1
    if up:
        parts = parts[:-up] if up <= len(parts) else []
    if module:
        parts.extend(module.split("."))
    return ".".join(p for p in parts if p)


def _package_of(path: Path) -> str:
    rel = path.relative_to(APP_DIR)
    if len(rel.parts) == 1:
        return ""
    return ".".join(rel.parts[:-1])


def _node(nid: str, label: str, kind: str, note: str) -> dict[str, str]:
    return {"id": nid, "label": label, "kind": kind, "note": note}


def _edge(src: str, dst: str) -> dict[str, str]:
    return {"id": f"{src}->{dst}", "source": src, "target": dst}
