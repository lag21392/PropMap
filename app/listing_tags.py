from __future__ import annotations

import json
import os
import re
from typing import Iterable

from .features import FEATURE_RULES, extract_features
from .geo import fold
from .models import Listing

META_KEY = "llm_tag_vocab"
MAX_LEARNED = 80
MAX_ITEM = 16
# Atributos de aviso, no lugares: sirven para comparar propiedades entre sí.
EXTRA_TAGS = (
    "a estrenar",
    "en pozo",
    "en construcción",
    "al frente",
    "contrafrente",
    "interno",
    "monoambiente",
    "suite",
    "vestidor",
    "dependencia",
    "baulera",
    "sum",
    "gimnasio",
    "mascotas",
    "apto profesional",
    "terraza propia",
    "lote propio",
    "semipiso",
    "dúplex",
    "toilete",
    "hidromasaje",
    "solarium",
    "laundry",
    "alarma",
    "grupo electrógeno",
)

_ALIAS: dict[str, str] = {}
_SEED: list[str] = []
for _label, _keys in FEATURE_RULES:
    _SEED.append(_label)
    _ALIAS[fold(_label)] = _label
    for _key in _keys:
        _ALIAS.setdefault(fold(_key), _label)
for _label in EXTRA_TAGS:
    if fold(_label) not in _ALIAS:
        _SEED.append(_label)
        _ALIAS[fold(_label)] = _label

_PLACE_HINT = re.compile(r"\d|[|/]|,\s|\s+y\s+", re.I)


def catalog() -> list[str]:
    seen: dict[str, str] = {}
    for label in (*_SEED, *_learned()):
        token = fold(label)
        if token and token not in seen:
            seen[token] = label
    return list(seen.values())


def catalog_text(limit: int = 56) -> str:
    return ", ".join(catalog()[:limit])


def tags_for_prompt(blob: str, limit: int = 12) -> str:
    text = fold(blob)
    if not text:
        return ""
    hits = [tag for tag in catalog() if fold(tag) in text]
    return ", ".join(hits[:limit])


def normalize_tag(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text or len(text) > 40:
        return None
    if _PLACE_HINT.search(text):
        return None
    token = fold(text)
    if len(token) < 3:
        return None
    return _ALIAS.get(token) or text.lower()


def merge_tags(*groups: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group or ():
            label = normalize_tag(str(raw))
            if not label:
                continue
            key = fold(label)
            if key in seen:
                continue
            seen.add(key)
            out.append(label)
            if len(out) >= MAX_ITEM:
                return out
    return out


def remember(tags: Iterable[str]) -> None:
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    fresh = [tag for tag in merge_tags(list(tags)) if fold(tag) not in _ALIAS]
    if not fresh:
        return
    try:
        from . import store

        learned = _learned()
        have = {fold(item) for item in learned}
        changed = False
        for tag in fresh:
            key = fold(tag)
            if key in have:
                continue
            learned.append(tag)
            have.add(key)
            changed = True
        if changed:
            store.set_meta(META_KEY, json.dumps(learned[:MAX_LEARNED], ensure_ascii=False))
    except Exception:
        return


def apply_tags(item: Listing, data: dict) -> list[str]:
    extra = dict(item.extra or {})
    blob = " ".join(
        p
        for p in (
            item.title,
            item.address,
            item.description,
            extra.get("pdf_text") or "",
        )
        if p
    )
    tags = merge_tags(
        extra.get("tags"),
        extra.get("amenities"),
        data.get("tags") if isinstance(data.get("tags"), list) else None,
        data.get("amenities") if isinstance(data.get("amenities"), list) else None,
        extract_features(blob),
    )
    extra["tags"] = tags
    extra["amenities"] = tags
    item.extra = extra
    remember(tags)
    return tags


def _learned() -> list[str]:
    try:
        from . import store

        raw = store.get_meta(META_KEY, "[]")
        data = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]
