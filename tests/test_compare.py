"""Tests for the /api/compare endpoint."""
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_compare_endpoint_returns_listings():
    """Test that /api/compare returns listings for valid IDs."""
    # Use known listing IDs from the database
    ids = "mercadolibre:MLA3896323580,mercadolibre:MLA1471561567"
    response = client.get(f"/api/compare?ids={ids}")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "listings" in data
    assert len(data["listings"]) == 2
    for listing in data["listings"]:
        assert "id" in listing
        assert "title" in listing
        assert "price_usd" in listing


def test_compare_endpoint_handles_invalid_ids():
    """Test that /api/compare handles invalid IDs gracefully."""
    ids = "invalid:id1,invalid:id2"
    response = client.get(f"/api/compare?ids={ids}")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["listings"] == []
    assert data["count"] == 0


def test_compare_endpoint_limits_to_four():
    """Test that /api/compare limits to 4 listings max."""
    # Use 5 known IDs
    ids = "mercadolibre:MLA3896323580,mercadolibre:MLA1471561567,mercadolibre:MLA3895996128,mercadolibre:MLA1566409437,mercadolibre:MLA1470334043"
    response = client.get(f"/api/compare?ids={ids}")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert len(data["listings"]) <= 4


def test_compare_endpoint_requires_ids_param():
    """Test that /api/compare requires ids parameter."""
    response = client.get("/api/compare")
    assert response.status_code == 422  # Validation error