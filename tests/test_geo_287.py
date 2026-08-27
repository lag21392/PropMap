from app.geo import _LEARNED, infer_barrio, locate, remember_city_polygons

TITLE = "Casa en Venta en Puerto Madryn, Biedma CASA DE 3 DORMITORIOS (Bº 287 VIVIENDAS)"
ADDR = "Angelo Mistrangelo 1200"


def test_infers_287_viviendas_from_title():
    remember_city_polygons("puerto-madryn", [])
    _LEARNED["puerto-madryn"] = []
    barrio, zona, _lat, _lon = infer_barrio(TITLE, ADDR, city="puerto-madryn")
    assert barrio == "287 Viviendas"
    assert zona == "Zona Norte"


def test_mistrangelo_keeps_barrio_from_text():
    remember_city_polygons("puerto-madryn", [])
    barrio, zona, lat, lon, exact, _kind = locate(
        "argenprop:19444640",
        -42.7548,
        -65.0592,
        TITLE,
        ADDR,
        "",
        city="puerto-madryn",
    )
    assert barrio == "287 Viviendas"
    assert zona == "Zona Norte"
    assert exact is False
    assert abs(lat + 42.7548) < 1e-4
    assert abs(lon + 65.0592) < 1e-4
