from app.geo import apply_public_location, listing_coords_are_exact, snap_to_approx_cell
from app.models import Listing
from app.scrapers import attach_location_facts, locate_item
from app.scrapers.details import _set_address


def test_properati_portal_pin_without_address_stays_approx_on_the_map():
    assert listing_coords_are_exact("properati", True) is False
    assert listing_coords_are_exact("zonaprop", True) is True
    data = apply_public_location({
        "source": "properati",
        "has_exact_location": True,
        "lat": -42.76921,
        "lon": -65.03851,
    })
    assert data["location_approx"] is True
    assert data["has_exact_location"] is False
    assert data["approx_span_m"] == 180
    assert data["approx_cell"]
    snapped = snap_to_approx_cell(-42.76921, -65.03851)
    assert data["lat"] == snapped[0]
    assert data["lon"] == snapped[1]


def test_geocoded_street_is_exact_even_on_properati():
    assert listing_coords_are_exact("properati", False, location_kind="exact") is True
    assert listing_coords_are_exact("zonaprop", False, location_kind="exact") is True
    data = apply_public_location({
        "source": "properati",
        "has_exact_location": False,
        "location_kind": "exact",
        "lat": -34.590238,
        "lon": -58.391935,
    })
    assert data["location_approx"] is False
    assert data["has_exact_location"] is True
    assert data["lat"] == -34.590238
    assert data["lon"] == -58.391935


def test_intersection_pin_is_round_on_the_map():
    data = apply_public_location({
        "source": "zonaprop",
        "has_exact_location": True,
        "location_kind": "intersection",
        "lat": -42.77,
        "lon": -65.04,
    })
    assert data["location_approx"] is False
    assert data["has_exact_location"] is True


def test_ungocoded_intersection_stays_approx_if_only_portal_cell():
    data = apply_public_location({
        "source": "zonaprop",
        "has_exact_location": False,
        "location_kind": "intersection",
        "lat": -42.77,
        "lon": -65.04,
    })
    assert data["location_approx"] is True


def test_street_pin_stays_round_for_other_portals():
    data = apply_public_location({
        "source": "argenprop",
        "has_exact_location": True,
        "lat": -42.7548,
        "lon": -65.0592,
    })
    assert data["location_approx"] is False
    assert data["has_exact_location"] is True
    assert data["lat"] == -42.7548


def test_snap_is_stable():
    a = snap_to_approx_cell(-34.6037, -58.3816)
    b = snap_to_approx_cell(-34.60371, -58.38162)
    assert a == b
    c = snap_to_approx_cell(-34.6037 + 0.0002, -58.3816)
    assert c == a


def test_locate_item_marks_properati_without_street_as_approx():
    item = Listing(
        source="properati",
        source_id="p1",
        url="https://www.properati.com.ar/x",
        title="Departamento en Palermo",
        property_type="departamento",
        address="Palermo, Capital Federal",
        lat=-34.588,
        lon=-58.430,
        city="microcentro-caba",
    )
    locate_item(item)
    assert item.has_exact_location is False


def test_locate_keeps_street_intersection_and_between():
    item = Listing(
        source="zonaprop",
        source_id="both",
        url="https://example.com/both",
        title="Depto en esquina Roca y Apeleg",
        property_type="departamento",
        address="Roca 240. Entre Mitre y 25 de Mayo",
        city="puerto-madryn",
    )
    attach_location_facts(item)
    assert "roca" in (item.extra.get("street") or "")
    assert item.extra.get("street_number") == 240
    assert "apeleg" in (item.extra.get("intersection") or "")
    assert item.extra.get("between")
    locate_item(item)
    assert "240" in (item.address or "")
    assert "apeleg" in (item.extra.get("intersection") or "")
    assert item.extra.get("between")
    public = item.to_public_dict()
    assert public["intersection"]
    assert public["between"]


def test_set_address_does_not_replace_street_with_intersection():
    item = Listing(
        source="zonaprop",
        source_id="keep-st",
        url="https://example.com/keep",
        title="Depto",
        property_type="departamento",
        address="Florida 600",
        city="caba",
    )
    _set_address(item, "Roca y Apeleg")
    assert "Florida 600" in item.address
    assert "apeleg" in (item.extra.get("intersection") or "").lower()


def test_set_address_keeps_street_and_stores_approx_label():
    item = Listing(
        source="properati",
        source_id="keep-approx",
        url="https://example.com/approx",
        title="Depto",
        property_type="departamento",
        address="Florida 600",
        city="caba",
    )
    _set_address(item, "Microcentro, Capital Federal")
    assert "Florida 600" in item.address
    assert "microcentro" in (item.extra.get("approx_address") or "").lower()
