from app.geo import infer_barrio, locate

TITLE = "Casa en Venta en Puerto Madryn, Biedma CASA DE 3 DORMITORIOS (Bº 287 VIVIENDAS)"
ADDR = "Angelo Mistrangelo 1200"


def test_infers_287_viviendas_from_title():
    barrio, zona, lat, lon = infer_barrio(TITLE, ADDR, city="puerto-madryn")
    assert barrio == "287 Viviendas"
    assert zona == "Zona Norte"
    assert lon < -65.05


def test_mistrangelo_is_not_dropped_on_city_center():
    barrio, zona, lat, lon, exact = locate(
        "argenprop:19444640",
        -42.775128853754936,
        -65.0385,
        TITLE,
        ADDR,
        "",
        city="puerto-madryn",
    )
    assert barrio == "287 Viviendas"
    assert zona == "Zona Norte"
    assert abs(lon + 65.0385) > 0.01
    assert lat > -42.77
