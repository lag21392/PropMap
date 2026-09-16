from fastapi import HTTPException

from app.search_auth import password_matches, require_search_password


def test_accepts_configured_password(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    require_search_password("test-secret")
    assert password_matches("test-secret")


def test_rejects_wrong_and_empty(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    assert not password_matches(None)
    assert not password_matches("")
    assert not password_matches("00000000")
    try:
        require_search_password("nope")
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("expected 401")


def test_rejects_when_password_missing(monkeypatch):
    monkeypatch.delenv("SEARCH_PASSWORD", raising=False)
    monkeypatch.delenv("MATOMO_PASSWORD", raising=False)
    assert not password_matches("test-secret")
    assert not password_matches("")


def test_accepts_matomo_password(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "ops-secret")
    monkeypatch.setenv("MATOMO_PASSWORD", "matomo-secret")
    assert password_matches("ops-secret")
    assert password_matches("matomo-secret")
    assert not password_matches("nope")
