from app import routing

ZP = "www.zonaprop.com.ar"


def _note(way: str, ok: bool, n: int, host: str = ZP, kind: str = "ficha") -> None:
    for _ in range(n):
        routing.note(host, kind, way, ok)


def test_without_data_the_fixed_order_wins():
    routing.reset()
    assert routing.best(ZP, "ficha", "translate") == "translate"
    assert routing.best(ZP, "lista", "tor") == "tor"
    assert routing.order(ZP, "ficha", "translate") == ["translate", "tor", "local"]


def test_a_couple_of_failures_do_not_change_the_route():
    """Dos fallos son ruido: recién con varios intentos se mueve el orden."""
    routing.reset()
    _note("translate", False, 2)
    assert routing.best(ZP, "ficha", "translate") == "translate"


def test_route_switches_when_the_usual_method_keeps_failing():
    routing.reset()
    _note("translate", False, 6)
    _note("tor", True, 4)
    assert routing.best(ZP, "ficha", "translate") == "tor"


def test_route_comes_back_when_the_replacement_also_falls():
    routing.reset()
    _note("translate", False, 6)
    _note("tor", True, 4)
    assert routing.best(ZP, "ficha", "translate") == "tor"
    _note("tor", False, 10)
    _note("translate", True, 6)
    assert routing.best(ZP, "ficha", "translate") == "translate"


def test_ficha_and_lista_are_measured_apart():
    """El traductor sirve para una ficha y casi nunca para un listado."""
    routing.reset()
    _note("translate", False, 8, kind="lista")
    _note("tor", True, 4, kind="lista")
    assert routing.best(ZP, "lista", "tor") == "tor"
    assert routing.best(ZP, "ficha", "translate") == "translate"


def test_each_portal_decides_on_its_own():
    routing.reset()
    _note("translate", False, 6)
    _note("tor", True, 4)
    assert routing.best(ZP, "ficha", "translate") == "tor"
    assert routing.best("www.properati.com.ar", "ficha", "translate") == "translate"


def test_old_results_stop_counting(monkeypatch):
    import time

    routing.reset()
    _note("translate", False, 6)
    _note("tor", True, 4)
    assert routing.best(ZP, "ficha", "translate") == "tor"
    later = time.time() + routing.WINDOW_SEC + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert routing.best(ZP, "ficha", "translate") == "translate"


def test_way_of_lane_maps_every_carril():
    assert routing.way_of_lane("translate") == "translate"
    assert routing.way_of_lane("tor-3") == "tor"
    assert routing.way_of_lane("proxy-0") == "tor"
    assert routing.way_of_lane("direct") == "local"
    assert routing.way_of_lane("urllib") == "local"
    assert routing.way_of_lane("stealth") == ""


def test_note_ignores_unknown_way_and_empty_host():
    routing.reset()
    _note("stealth", True, 5)
    routing.note("", "ficha", "tor", True)
    assert routing.snapshot()["rows"] == []
    assert routing.best(ZP, "ficha", "translate") == "translate"


def test_snapshot_reports_success_rate_per_portal():
    routing.reset()
    _note("translate", True, 3)
    _note("translate", False, 1)
    rows = routing.snapshot()["rows"]
    assert rows == [{"host": ZP, "kind": "ficha", "way": "translate", "n": 4, "ok_pct": 75}]
