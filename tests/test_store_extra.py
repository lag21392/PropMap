from app.store import _loads_extra


def test_loads_extra_map_row_keeps_place_and_drops_heavy():
    raw = (
        '{"search_city":"caba","place_tags":["CABA"],'
        '"access":{"nearby":[{"name":"subte"}]},'
        '"photos":["http://x"],'
        '"llm":{"city":"CABA"},'
        '"pdf_text":"hola",'
        '"profile":{"pin_grade":"A","axes":{"walk":{"score":8,"pois":[1,2],"note":"ok"}}}}'
    )
    extra = _loads_extra(raw, map_row=True)
    assert extra["search_city"] == "caba"
    assert extra["place_tags"] == ["CABA"]
    assert "access" not in extra
    assert "photos" not in extra
    assert "llm" not in extra
    assert "pdf_text" not in extra
    assert extra["profile"]["pin_grade"] == "A"
    assert extra["profile"]["axes"]["walk"] == {"score": 8, "confidence": None, "note": "ok"}


def test_loads_extra_full_keeps_access():
    extra = _loads_extra('{"access":{"score":1},"search_city":"pilar"}', map_row=False)
    assert extra["access"] == {"score": 1}
    assert extra["search_city"] == "pilar"


def test_map_fetch_keeps_pentagon_scores(tmp_path, monkeypatch):
    from app import store
    from app.models import Listing

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    store.upsert_many(
        [
            Listing(
                source="zonaprop",
                source_id="radar",
                url="https://example.com/radar",
                title="Depto",
                property_type="departamento",
                city="caba",
                extra={
                    "profile": {
                        "pin_grade": "exact",
                        "axes": {
                            "zona": {"score": 80, "pois": [1, 2], "note": "Palermo"},
                            "price_m2": {"score": 70, "confidence": "high"},
                        },
                    },
                    "access": {"nearby": [{"name": "subte"}]},
                },
            )
        ]
    )
    rows = store.fetch_by_cities({"caba"})
    assert len(rows) == 1
    axes = rows[0].extra["profile"]["axes"]
    assert axes["zona"] == {"score": 80, "confidence": None, "note": "Palermo"}
    assert axes["price_m2"]["score"] == 70
    assert "access" not in rows[0].extra


def test_upsert_many_merges_existing_and_keeps_last_duplicate(tmp_path, monkeypatch):
    from app import store
    from app.models import Listing

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()

    def item(source_id: str, **kw) -> Listing:
        extra = dict(kw.pop("extra", None) or {})
        return Listing(
            source="zonaprop",
            source_id=source_id,
            url=kw.pop("url", f"https://example.com/{source_id}"),
            title=kw.pop("title", "Depto"),
            property_type="departamento",
            city="caba",
            extra=extra,
            **kw,
        )

    store.upsert_many(
        [
            item("u1", description="descripcion larga original", extra={"amenities": ["pileta"]}),
            item("u2", title="Casa", description="casa"),
        ]
    )
    store.upsert_many(
        [item("u1", description="corta", extra={"street": "Mitre"})]
    )
    got = store.get_listing("zonaprop:u1")
    assert got.description == "descripcion larga original"
    assert got.extra.get("street") == "Mitre"
    assert "pileta" in (got.extra.get("amenities") or [])

    store.upsert_many(
        [
            item("u2", title="Casa", extra={"k": "a"}),
            item("u2", title="Casa nueva", extra={"k": "b"}),
        ]
    )
    got2 = store.get_listing("zonaprop:u2")
    assert got2.extra.get("k") == "b"

    got2.score = 77.0
    got2.deal_label = "oportunidad"
    store.update_scores([got2])
    scored = store.get_listing("zonaprop:u2")
    assert scored.score == 77.0
    assert scored.deal_label == "oportunidad"
