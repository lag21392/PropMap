def test_connect_reuses_the_same_sqlite_connection(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    with store.connect() as first:
        id_first = id(first)
        first.execute("SELECT 1")
    with store.connect() as second:
        id_second = id(second)
        second.execute("SELECT 1")
    assert id_first == id_second


def test_connect_opens_new_file_when_db_path_changes(tmp_path, monkeypatch):
    from app import store

    first_path = tmp_path / "a.sqlite"
    second_path = tmp_path / "b.sqlite"
    monkeypatch.setattr(store, "DB_PATH", first_path)
    store.init()
    with store.connect() as conn:
        conn.execute("CREATE TABLE extra (n INTEGER)")
        conn.execute("INSERT INTO extra VALUES (7)")
    monkeypatch.setattr(store, "DB_PATH", second_path)
    store.init()
    with store.connect() as conn:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "extra" not in names
    monkeypatch.setattr(store, "DB_PATH", first_path)
    with store.connect() as conn:
        n = conn.execute("SELECT n FROM extra").fetchone()[0]
    assert n == 7
