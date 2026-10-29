"""Guided one-product-at-a-time intake (plan 2026-10-29).

Task 1: session store + item grounding (phantom fix).
"""

from app.tools import (get_intake_session, ground_intake_items,
                       save_intake_session)


def test_grounding_drops_phantom_item():
    items = [{"product": "Albany Bread"}, {"product": "Photo item 4"}]
    products = [{"brand": "Albany", "variant": "Bread", "size": "",
                 "visible_count": 1, "readable": True, "notes": ""}]
    kept = ground_intake_items(items, products, "please add it to our stock")
    assert [e["product"] for e in kept] == ["Albany Bread"]


def test_session_roundtrip_and_isolation(conn):
    save_intake_session(conn, "s1", {"current": 1, "items_json": []})
    assert get_intake_session(conn, "s1")["current"] == 1
    assert get_intake_session(conn, "s2") is None
