from app.geo import barrio_containing, barrio_overlays, infer_barrio, locate, remember_city_polygons
from app.models import Listing


CHIQUI_LAT = -42.7838439
CHIQUI_LON = -65.020025

DESEMBARCO_RING = [
    [-42.7875, -65.0265],
    [-42.7875, -65.0140],
    [-42.7820, -65.0125],
    [-42.7795, -65.0160],
    [-42.7795, -65.0265],
    [-42.7875, -65.0265],
]
MEDANOS_RING = [
    [-42.7860, -65.0060],
    [-42.7860, -64.9900],
    [-42.7780, -64.9900],
    [-42.7780, -65.0060],
    [-42.7860, -65.0060],
]


def _inject_madryn_osm() -> None:
    remember_city_polygons(
        "puerto-madryn",
        [
            {
                "name": "Desembarco",
                "zona": "Zona Sur",
                "lat": -42.7835,
                "lon": -65.0195,
                "ring": DESEMBARCO_RING,
            },
            {
                "name": "Los Médanos",
                "zona": "Zona Sur",
                "lat": -42.7820,
                "lon": -64.9980,
                "ring": MEDANOS_RING,
            },
        ],
    )


def test_chiquichan_is_desembarco_not_medanos():
    _inject_madryn_osm()
    barrio, zona, lat, lon, exact, _kind = locate(
        "zonaprop:59769173",
        CHIQUI_LAT,
        CHIQUI_LON,
        "Departamento en Venta Puerto Madryn",
        "Chiquichan",
        "",
        city="puerto-madryn",
    )
    assert exact is True
    assert barrio == "Desembarco"
    assert zona == "Zona Sur"
    assert abs(lat - CHIQUI_LAT) < 1e-6
    assert barrio_containing(CHIQUI_LAT, CHIQUI_LON, city="puerto-madryn")[0] == "Desembarco"


def test_chiquichan_portal_exact_beats_numbers_in_the_description():
    _inject_madryn_osm()
    barrio, zona, lat, lon, exact, kind = locate(
        "zonaprop:59769173",
        CHIQUI_LAT,
        CHIQUI_LON,
        "Departamento en Venta Puerto Madryn",
        "Chiquichan",
        "Excelente monoambiente de 63 m². Saldo en 24 cuotas de USD 1925",
        city="puerto-madryn",
        extracted={"portal_exact": True},
    )
    assert exact is True
    assert kind == "saved"
    assert barrio == "Desembarco"
    assert abs(lat - CHIQUI_LAT) < 1e-6
    assert abs(lon - CHIQUI_LON) < 1e-6


def test_chiquichan_text_maps_to_desembarco():
    _inject_madryn_osm()
    barrio, zona, _lat, _lon = infer_barrio(
        "Depto en barrio Desembarco",
        "Chiquichan",
        city="puerto-madryn",
    )
    assert barrio == "Desembarco"


def test_madryn_overlays_are_real_polygons_not_circles():
    _inject_madryn_osm()
    items = [
        Listing(
            source="t",
            source_id="des",
            url="https://example.com",
            title="Depto Chiquichan",
            property_type="departamento",
            barrio="Desembarco",
            zona="Zona Sur",
            city="puerto-madryn",
            lat=CHIQUI_LAT,
            lon=CHIQUI_LON,
            has_exact_location=True,
        ),
        Listing(
            source="t",
            source_id="med",
            url="https://example.com",
            title="Casa Médanos",
            property_type="casa",
            barrio="Los Médanos",
            zona="Zona Sur",
            city="puerto-madryn",
            lat=-42.7820,
            lon=-64.9980,
            has_exact_location=True,
        ),
    ]
    overlays = {row["name"]: row for row in barrio_overlays(items, city="puerto-madryn")}
    assert "Desembarco" in overlays
    assert "Los Médanos" in overlays
    des = overlays["Desembarco"]["ring"]
    med = overlays["Los Médanos"]["ring"]
    assert len(des) > 4
    radii = []
    clon = overlays["Desembarco"]["lon"]
    clat = overlays["Desembarco"]["lat"]
    for lat, lon in des[:-1]:
        radii.append((lat - clat) ** 2 + (lon - clon) ** 2)
    assert max(radii) / (min(radii) or 1e-12) > 1.4
    from app.geo import _point_in_ring

    assert _point_in_ring(CHIQUI_LAT, CHIQUI_LON, des) is True
    assert _point_in_ring(CHIQUI_LAT, CHIQUI_LON, med) is False


def test_overlays_stretch_to_neighbor_barrio():
    remember_city_polygons("cordoba", [])
    west = [
        Listing(
            source="t",
            source_id=f"w{i}",
            url="https://example.com",
            title="Casa oeste",
            property_type="casa",
            barrio="Nueva Córdoba",
            zona="Zona Sur",
            city="cordoba",
            lat=-31.424,
            lon=-64.190 + i * 0.001,
            has_exact_location=True,
        )
        for i in range(3)
    ]
    east = [
        Listing(
            source="t",
            source_id=f"e{i}",
            url="https://example.com",
            title="Casa este",
            property_type="casa",
            barrio="Güemes",
            zona="Zona Este",
            city="cordoba",
            lat=-31.424,
            lon=-64.170 + i * 0.001,
            has_exact_location=True,
        )
        for i in range(3)
    ]
    overlays = {row["name"]: row for row in barrio_overlays(west + east, city="cordoba")}
    assert "Nueva Córdoba" in overlays
    assert "Güemes" in overlays
    from app.geo import _point_in_ring

    assert _point_in_ring(-31.424, -64.190, overlays["Nueva Córdoba"]["ring"]) is True
    assert _point_in_ring(-31.424, -64.170, overlays["Güemes"]["ring"]) is True
    assert _point_in_ring(-31.424, -64.190, overlays["Güemes"]["ring"]) is False
    west_lons = [p[1] for p in overlays["Nueva Córdoba"]["ring"][:-1]]
    east_lons = [p[1] for p in overlays["Güemes"]["ring"][:-1]]
    assert len(overlays["Nueva Córdoba"]["ring"]) >= 4
    assert abs(max(west_lons) - min(east_lons)) < 0.006


def test_barrios_for_is_cached_until_polygons_change():
    from app.geo import barrios_for, remember_city_polygons

    remember_city_polygons(
        "ciudad-cache-test",
        [{"name": "Alpha", "zona": "Norte", "lat": 0.0, "lon": 0.0}],
    )
    first = barrios_for("ciudad-cache-test")
    second = barrios_for("ciudad-cache-test")
    assert first is second
    remember_city_polygons(
        "ciudad-cache-test",
        [
            {"name": "Alpha", "zona": "Norte", "lat": 0.0, "lon": 0.0},
            {"name": "Beta", "zona": "Sur", "lat": 1.0, "lon": 1.0},
        ],
    )
    third = barrios_for("ciudad-cache-test")
    assert third is not first
    assert {row["name"] for row in third} == {"Alpha", "Beta"}


def test_fold_short_cache_matches_raw():
    from app.geo import _fold_raw, _fold_short, fold

    _fold_short.cache_clear()
    assert fold("Palermo") == "palermo"
    assert fold("Palermo") == _fold_raw("Palermo")
    long = ("áéí " * 40)
    assert len(long) > 96
    assert fold(long) == _fold_raw(long)
    from app.geo import _fold_long

    _fold_long.cache_clear()
    blob = "Departamento en venta " * 20
    assert 96 < len(blob) <= 4000
    assert fold(blob) == _fold_raw(blob)
    assert fold(blob) == _fold_long(blob)


def test_barrio_containing_bbox_skips_far_rings_and_keeps_smallest():
    from app.geo import _polygon_index, remember_city_polygons

    remember_city_polygons(
        "ciudad-bbox-test",
        [
            {
                "name": "Grande",
                "zona": "Norte",
                "lat": 0.0,
                "lon": 0.0,
                "ring": [[-1, -1], [-1, 1], [1, 1], [1, -1], [-1, -1]],
            },
            {
                "name": "Chico",
                "zona": "Norte",
                "lat": 0.0,
                "lon": 0.0,
                "ring": [[-0.2, -0.2], [-0.2, 0.2], [0.2, 0.2], [0.2, -0.2], [-0.2, -0.2]],
            },
            {
                "name": "Lejos",
                "zona": "Sur",
                "lat": 10.0,
                "lon": 10.0,
                "ring": [[9, 9], [9, 11], [11, 11], [11, 9], [9, 9]],
            },
        ],
    )
    inner = barrio_containing(0.0, 0.0, city="ciudad-bbox-test")
    assert inner is not None
    assert inner[0] == "Chico"
    assert barrio_containing(10.0, 10.0, city="ciudad-bbox-test")[0] == "Lejos"
    assert barrio_containing(50.0, 50.0, city="ciudad-bbox-test") is None
    index = _polygon_index("ciudad-bbox-test")
    assert len(index) == 3
    remember_city_polygons(
        "ciudad-bbox-test",
        [
            {
                "name": "Solo",
                "zona": "Centro",
                "lat": 0.0,
                "lon": 0.0,
                "ring": [[-0.5, -0.5], [-0.5, 0.5], [0.5, 0.5], [0.5, -0.5], [-0.5, -0.5]],
            }
        ],
    )
    assert barrio_containing(0.0, 0.0, city="ciudad-bbox-test")[0] == "Solo"
    assert len(_polygon_index("ciudad-bbox-test")) == 1
