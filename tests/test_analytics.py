from app.analytics import record, summary


def test_analytics_keeps_pageviews_and_places():
    record({"n": "pageview", "p": "/", "vid": "t-visitor", "city": ""}, ua="Mozilla")
    record({"n": "place", "p": "/", "vid": "t-visitor", "city": "microcentro-caba"}, ua="iPhone")
    data = summary(14)
    assert data["events"] >= 2
    assert data["visitors"] >= 1
    names = {row["name"] for row in data["events_by_name"]}
    assert "pageview" in names or "place" in names


def test_analytics_counts_where_they_came_from():
    from app import analytics

    analytics._recent.clear()
    record({"n": "pageview", "p": "/from-google", "vid": "g1", "r": "https://www.google.com/search"}, ua="Mozilla")
    record({"n": "pageview", "p": "/direct", "vid": "d1"}, ua="Mozilla")
    record({"n": "pageview", "p": "/utm", "vid": "i1", "utm": "instagram"}, ua="Mozilla")
    data = summary(14)
    refs = {row["name"]: row["count"] for row in data["referrers"]}
    assert refs.get("www.google.com") >= 1
    assert refs.get("directo") >= 1
    assert refs.get("instagram") >= 1
    assert data["visitors"] >= 1
    assert data["pageviews"] >= 1
