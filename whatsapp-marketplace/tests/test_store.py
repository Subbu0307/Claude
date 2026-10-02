import pytest

from marketplace.store import MarketplaceError


def test_order_reserves_and_cancel_restores_stock(store):
    store.ensure_user("111", "Seller")
    store.ensure_user("222", "Buyer")
    listing = store.create_listing("111", "Bike", "Good", "sports", 5000, 2)

    order = store.place_order("222", listing["id"], 2)
    assert order["total_cents"] == 10000
    assert store.get_listing(listing["id"])["status"] == "sold_out"
    assert store.search_listings("bike") == []

    store.update_order_status("111", order["id"], "cancelled")
    after = store.get_listing(listing["id"])
    assert after["quantity"] == 2 and after["status"] == "active"


def test_order_rules(store):
    store.ensure_user("111")
    store.ensure_user("222")
    store.ensure_user("333")
    listing = store.create_listing("111", "Lamp", "", "home", 1000, 1)

    with pytest.raises(MarketplaceError, match="own listing"):
        store.place_order("111", listing["id"], 1)
    with pytest.raises(MarketplaceError, match="Only 1 left"):
        store.place_order("222", listing["id"], 5)

    order = store.place_order("222", listing["id"], 1)
    with pytest.raises(MarketplaceError, match="not part of this order"):
        store.update_order_status("333", order["id"], "cancelled")
    with pytest.raises(MarketplaceError, match="can change it to"):
        store.update_order_status("222", order["id"], "completed")  # buyer can't complete before shipping

    for status in ("accepted", "shipped"):
        store.update_order_status("111", order["id"], status)
    assert store.update_order_status("222", order["id"], "completed")["status"] == "completed"


def test_only_owner_can_edit_listing(store):
    store.ensure_user("111")
    store.ensure_user("222")
    listing = store.create_listing("111", "Desk", "", "furniture", 3000, 1)
    with pytest.raises(MarketplaceError, match="own listings"):
        store.update_listing("222", listing["id"], price_cents=1)
    assert store.update_listing("111", listing["id"], price_cents=2500)["price_cents"] == 2500


def test_search_filters(store):
    store.ensure_user("111")
    store.create_listing("111", "Red sofa", "Comfy", "furniture", 20000, 1)
    store.create_listing("111", "Blue chair", "Wooden", "furniture", 4000, 1)
    store.create_listing("111", "Phone", "Android", "electronics", 15000, 1)

    assert {l["title"] for l in store.search_listings(category="furniture")} == {"Red sofa", "Blue chair"}
    assert [l["title"] for l in store.search_listings(max_price_cents=5000)] == ["Blue chair"]
    assert [l["title"] for l in store.search_listings("comfy")] == ["Red sofa"]


def test_mark_processed_is_idempotent(store):
    assert store.mark_processed("wamid.1") is True
    assert store.mark_processed("wamid.1") is False
