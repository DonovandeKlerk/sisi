"""DB schema, money formatting, and date-freeze helpers."""

from app.db import cents_to_rand, init_db, today_str


def test_schema_tables_exist(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in [
        "shops", "customers", "products", "credit_entries", "payments",
        "sales", "stock_moves", "expiry_items", "pending_actions",
        "actions", "messages_seen", "conversation_turns",
    ]:
        assert expected in tables


def test_shop_customer_product_roundtrip(conn):
    conn.execute("INSERT INTO shops (shop_id, name) VALUES ('27710000001', 'Test Spaza')")
    cur = conn.execute("INSERT INTO customers (shop_id, name) VALUES ('27710000001', 'Sipho')")
    assert cur.lastrowid == 1
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, wholesaler) VALUES (?, ?, ?, ?)",
        ("27710000001", "Coke 2L", 3600, "Coke Depot"),
    )
    row = conn.execute("SELECT * FROM products WHERE name = 'Coke 2L'").fetchone()
    assert row["price_cents"] == 3600
    assert row["shop_id"] == "27710000001"


def test_customer_name_unique_per_shop_case_insensitive(conn):
    conn.execute("INSERT INTO shops (shop_id, name) VALUES ('s1', 'A')")
    conn.execute("INSERT INTO customers (shop_id, name) VALUES ('s1', 'Thandi')")
    try:
        conn.execute("INSERT INTO customers (shop_id, name) VALUES ('s1', 'thandi')")
        raise AssertionError("duplicate name should fail")
    except Exception:
        pass


def test_data_isolated_per_shop(conn):
    conn.execute("INSERT INTO shops (shop_id, name) VALUES ('s1', 'A')")
    conn.execute("INSERT INTO shops (shop_id, name) VALUES ('s2', 'B')")
    conn.execute("INSERT INTO customers (shop_id, name) VALUES ('s1', 'Sipho')")
    rows = conn.execute("SELECT * FROM customers WHERE shop_id = 's2'").fetchall()
    assert rows == []


def test_cents_to_rand():
    assert cents_to_rand(6600) == "R66.00"
    assert cents_to_rand(5) == "R0.05"
    assert cents_to_rand(0) == "R0.00"
    assert cents_to_rand(-150) == "-R1.50"


def test_today_freeze(monkeypatch):
    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    assert today_str() == "2026-11-01"
    monkeypatch.delenv("SISI_TODAY")
    assert len(today_str()) == 10  # real date, YYYY-MM-DD


def test_init_db_idempotent_and_preserves_legacy_rows(conn):
    conn.execute("INSERT INTO shops (shop_id, name) VALUES ('legacy-1', 'Old Shop')")
    conn.execute("INSERT INTO customers (shop_id, name) VALUES ('legacy-1', 'Sipho')")
    conn.commit()
    init_db(conn)  # rerun: ALTERs must pass harmlessly
    init_db(conn)
    shop = conn.execute("SELECT * FROM shops WHERE shop_id = 'legacy-1'").fetchone()
    assert shop["name"] == "Old Shop"
    assert shop["address"] == "" and shop["logo_path"] == ""
    cust = conn.execute("SELECT * FROM customers WHERE shop_id = 'legacy-1'").fetchone()
    assert cust["name"] == "Sipho" and cust["credit_limit_cents"] is None
