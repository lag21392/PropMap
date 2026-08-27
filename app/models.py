from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SOURCE_LABELS = {
    "zonaprop": "ZonaProp",
    "mercadolibre": "Mercado Libre",
    "properati": "Properati",
    "argenprop": "Argenprop",
    "manual": "Carga manual",
}


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    property_type: str
    price: float | None = None
    currency: str = "USD"
    price_usd: float | None = None
    address: str = ""
    barrio: str = "Sin clasificar"
    zona: str = "Sin clasificar"
    lat: float | None = None
    lon: float | None = None
    covered_m2: float | None = None
    total_m2: float | None = None
    rooms: int | None = None
    bedrooms: int | None = None
    bathrooms: float | None = None
    parking: int | None = None
    age_years: int | None = None
    image: str = ""
    publisher: str = ""
    description: str = ""
    published_at: str = ""
    price_m2: float | None = None
    score: float | None = None
    deal_label: str = ""
    vs_barrio_pct: float | None = None
    fingerprint: str = ""
    has_exact_location: bool = False
    favorite: bool = False
    notes: str = ""
    contacted: bool = False
    city: str = "caba"
    quality_score: float | None = None
    quality_label: str = ""
    details_scraped: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["id"] = self.id
        return data

    def to_public_dict(self) -> dict[str, Any]:
        from .geo import apply_public_location

        data = {
            "id": self.id,
            "source": self.source,
            "url": self.url,
            "title": self.title,
            "property_type": self.property_type,
            "price": self.price,
            "currency": self.currency,
            "price_usd": self.price_usd,
            "address": self.address,
            "barrio": self.barrio,
            "zona": self.zona,
            "lat": self.lat,
            "lon": self.lon,
            "covered_m2": self.covered_m2,
            "total_m2": self.total_m2,
            "rooms": self.rooms,
            "bedrooms": self.bedrooms,
            "bathrooms": self.bathrooms,
            "parking": self.parking,
            "age_years": self.age_years,
            "image": self.image,
            "publisher": self.publisher,
            "price_m2": self.price_m2,
            "score": self.score,
            "deal_label": self.deal_label,
            "vs_barrio_pct": self.vs_barrio_pct,
            "has_exact_location": self.has_exact_location,
            "favorite": False,
            "notes": "",
            "contacted": False,
            "city": self.city,
            "quality_score": self.quality_score,
            "quality_label": self.quality_label,
            "details_scraped": self.details_scraped,
            "amenities": (self.extra or {}).get("amenities") or [],
            "tags": (self.extra or {}).get("tags") or (self.extra or {}).get("amenities") or [],
            "expenses": (self.extra or {}).get("expenses"),
            "description": self.description or "",
            "published_at": self.published_at,
            "deal_score": (self.extra or {}).get("deal_score"),
            "is_outlier": bool((self.extra or {}).get("is_outlier")),
            "deal_reasons": (self.extra or {}).get("deal_reasons") or [],
            "monthly_rent_usd": (self.extra or {}).get("monthly_rent_usd"),
            "monthly_yield_pct": (self.extra or {}).get("monthly_yield_pct"),
            "nightly_usd": (self.extra or {}).get("nightly_usd"),
            "temporal_yield_pct": (self.extra or {}).get("temporal_yield_pct"),
            "occupancy_pct": (self.extra or {}).get("occupancy_pct"),
            "rental_score": (self.extra or {}).get("rental_score"),
            "rental_label": (self.extra or {}).get("rental_label") or "",
            "rental_reasons": (self.extra or {}).get("rental_reasons") or [],
            "rental_month_scope": (self.extra or {}).get("rental_month_scope") or "",
            "rental_night_scope": (self.extra or {}).get("rental_night_scope") or "",
            "rental_month_n": (self.extra or {}).get("rental_month_n") or 0,
            "rental_night_n": (self.extra or {}).get("rental_night_n") or 0,
            "data_fixes": (self.extra or {}).get("data_fixes") or [],
            "exclude_from_comps": bool((self.extra or {}).get("exclude_from_comps")),
            "location_kind": (self.extra or {}).get("location_kind") or "",
            "intersection": (self.extra or {}).get("intersection") or "",
            "between": (self.extra or {}).get("between") or "",
            "approx_address": (self.extra or {}).get("approx_address") or "",
            "street": (self.extra or {}).get("street") or "",
            "street_number": (self.extra or {}).get("street_number"),
            "portal_lat": (self.extra or {}).get("portal_lat"),
            "portal_lon": (self.extra or {}).get("portal_lon"),
            "mortgage_credit": (self.extra or {}).get("mortgage_credit"),
            "floor": (self.extra or {}).get("llm", {}).get("floor") if isinstance((self.extra or {}).get("llm"), dict) else None,
            "orientation": (self.extra or {}).get("llm", {}).get("orientation") if isinstance((self.extra or {}).get("llm"), dict) else None,
            "condition": (self.extra or {}).get("llm", {}).get("condition") if isinstance((self.extra or {}).get("llm"), dict) else None,
            "lot_m2": self.total_m2,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "sources": (self.extra or {}).get("sources") or [
                {
                    "id": self.id,
                    "source": self.source,
                    "url": self.url,
                    "label": SOURCE_LABELS.get(self.source, self.source),
                }
            ],
            "photos": (self.extra or {}).get("photos") or ([self.image] if self.image else []),
            "duplicate_of": (self.extra or {}).get("duplicate_of") or "",
        }
        return apply_public_location(data)
