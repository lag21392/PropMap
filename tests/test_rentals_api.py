from fastapi.testclient import TestClient

from app import main


def test_rentals_api_filters_by_city_and_period(monkeypatch):
    rows = [
        {
            "id": "rent:one",
            "city": "puerto-madryn",
            "period": "monthly",
            "price_usd": 500,
        },
        {
            "id": "rent:two",
            "city": "puerto-madryn",
            "period": "nightly",
            "price_usd": 80,
        },
        {
            "id": "rent:three",
            "city": "caba",
            "period": "monthly",
            "price_usd": 700,
        },
    ]
    monkeypatch.setattr("app.store.fetch_rentals", lambda city=None: rows)

    with TestClient(main.app) as client:
        response = client.get(
            "/api/rentals",
            params={"city": "puerto-madryn", "period": "monthly"},
        )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["listings"]] == ["rent:one"]


def test_rentals_api_accepts_weekly_period(monkeypatch):
    rows = [
        {
            "id": "rent:wk",
            "city": "caba",
            "period": "weekly",
            "price_usd": 570,
        },
    ]
    monkeypatch.setattr("app.store.fetch_rentals", lambda city=None: rows)

    with TestClient(main.app) as client:
        response = client.get("/api/rentals", params={"city": "caba", "period": "weekly"})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["listings"]] == ["rent:wk"]
