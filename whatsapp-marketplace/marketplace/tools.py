"""Tools the agent can call. Every call runs on behalf of the WhatsApp sender only.

The sender's phone number is bound server-side when the executor is built, so the
model can never act as (or read the private data of) another user.
"""

import json
from typing import Any, Callable

from .store import MarketplaceError, Store

Notify = Callable[[str, str], None]


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool(
        "get_my_profile",
        "Get the current user's marketplace profile (name, location).",
        {},
        [],
    ),
    _tool(
        "update_my_profile",
        "Set the current user's display name and/or location (city/neighbourhood) shown to other users.",
        {
            "name": {"type": "string", "description": "Display name."},
            "location": {"type": "string", "description": "City or neighbourhood, e.g. 'Koramangala, Bengaluru'."},
        },
        [],
    ),
    _tool(
        "search_listings",
        "Search active listings. Use whenever the user wants to browse or find something to buy. "
        "All filters are optional; an empty search returns the newest listings.",
        {
            "query": {"type": "string", "description": "Keywords matched against title, description and category."},
            "category": {"type": "string", "description": "Exact category, e.g. 'electronics', 'furniture'."},
            "max_price": {"type": "number", "description": "Maximum unit price in the marketplace currency."},
        },
        [],
    ),
    _tool(
        "get_listing",
        "Get full details for one listing by its id.",
        {"listing_id": {"type": "integer"}},
        ["listing_id"],
    ),
    _tool(
        "create_listing",
        "Publish a new listing for the current user to sell. Confirm title, price and quantity with the user first.",
        {
            "title": {"type": "string", "description": "Short title, e.g. 'iPhone 13, 128GB, blue'."},
            "description": {"type": "string", "description": "Condition, details, pickup/delivery info."},
            "category": {
                "type": "string",
                "description": "One lowercase word: electronics, furniture, clothing, books, home, vehicles, "
                "sports, toys, food, services or other.",
            },
            "price": {"type": "number", "description": "Unit price in the marketplace currency."},
            "quantity": {"type": "integer", "description": "Units available, at least 1."},
        },
        ["title", "description", "category", "price", "quantity"],
    ),
    _tool(
        "my_listings",
        "List the current user's own listings (active and sold out).",
        {},
        [],
    ),
    _tool(
        "update_listing",
        "Change price, quantity or description of one of the current user's listings, or remove it.",
        {
            "listing_id": {"type": "integer"},
            "price": {"type": "number"},
            "quantity": {"type": "integer"},
            "description": {"type": "string"},
            "remove": {"type": "boolean", "description": "true to take the listing down."},
        },
        ["listing_id"],
    ),
    _tool(
        "place_order",
        "Buy a listing for the current user. Reserves stock and notifies the seller on WhatsApp. "
        "Only call after the user has clearly confirmed the item, quantity and total price.",
        {
            "listing_id": {"type": "integer"},
            "quantity": {"type": "integer"},
            "note": {"type": "string", "description": "Optional message for the seller (pickup time, etc.)."},
        },
        ["listing_id", "quantity"],
    ),
    _tool(
        "my_orders",
        "List recent orders where the current user is the buyer or the seller.",
        {},
        [],
    ),
    _tool(
        "update_order_status",
        "Move an order forward. Sellers: pending->accepted|cancelled, accepted->shipped|cancelled. "
        "Buyers: pending->cancelled, shipped->completed. The other party is notified on WhatsApp.",
        {
            "order_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["accepted", "shipped", "completed", "cancelled"]},
        },
        ["order_id", "status"],
    ),
]


def _cents(amount: float) -> int:
    return int(round(amount * 100))


class ToolExecutor:
    """Executes tool calls for a single sender."""

    def __init__(self, store: Store, phone: str, notify: Notify, currency: str = "USD"):
        self.store = store
        self.phone = phone
        self.notify = notify
        self.currency = currency

    def run(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Returns (result_json, is_error)."""
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return json.dumps({"error": f"Unknown tool {name}"}), True
        try:
            return json.dumps(handler(**tool_input), default=str), False
        except MarketplaceError as e:
            return json.dumps({"error": str(e)}), True
        except (TypeError, ValueError) as e:
            return json.dumps({"error": f"Invalid input: {e}"}), True

    # ---- formatting helpers -------------------------------------------------

    def _money(self, cents: int) -> str:
        return f"{cents / 100:.2f} {self.currency}"

    def _public_listing(self, listing: dict[str, Any]) -> dict[str, Any]:
        out = {
            "id": listing["id"],
            "title": listing["title"],
            "description": listing["description"],
            "category": listing["category"],
            "price": self._money(listing["price_cents"]),
            "quantity_available": listing["quantity"],
            "status": listing["status"],
            "seller_name": listing.get("seller_name") or "Seller",
            "seller_location": listing.get("seller_location"),
        }
        if listing["seller_phone"] == self.phone:
            out["is_mine"] = True
        return out

    def _order_view(self, order: dict[str, Any]) -> dict[str, Any]:
        role = "buyer" if order["buyer_phone"] == self.phone else "seller"
        out = {
            "id": order["id"],
            "my_role": role,
            "listing_id": order["listing_id"],
            "item": order["listing_title"],
            "quantity": order["quantity"],
            "unit_price": self._money(order["unit_price_cents"]),
            "total": self._money(order["total_cents"]),
            "status": order["status"],
            "note": order["note"],
            "buyer_name": order["buyer_name"],
            "seller_name": order["seller_name"],
        }
        # Contact details are shared only once the seller accepts, so buyer and seller can arrange handover.
        if order["status"] in ("accepted", "shipped", "completed"):
            out["counterparty_whatsapp"] = "+" + (order["seller_phone"] if role == "buyer" else order["buyer_phone"])
        return out

    # ---- tools --------------------------------------------------------------

    def _t_get_my_profile(self) -> dict[str, Any]:
        user = self.store.get_user(self.phone) or {}
        return {"name": user.get("name"), "location": user.get("location")}

    def _t_update_my_profile(self, name: str | None = None, location: str | None = None) -> dict[str, Any]:
        user = self.store.update_profile(self.phone, name, location)
        return {"name": user["name"], "location": user["location"]}

    def _t_search_listings(self, query: str = "", category: str = "", max_price: float | None = None):
        results = self.store.search_listings(
            query=query, category=category, max_price_cents=_cents(max_price) if max_price is not None else None
        )
        return {"count": len(results), "listings": [self._public_listing(l) for l in results]}

    def _t_get_listing(self, listing_id: int) -> dict[str, Any]:
        return self._public_listing(self.store.get_listing(listing_id))

    def _t_create_listing(self, title: str, description: str, category: str, price: float, quantity: int):
        listing = self.store.create_listing(self.phone, title, description, category, _cents(price), quantity)
        return {"created": self._public_listing(listing)}

    def _t_my_listings(self) -> dict[str, Any]:
        listings = self.store.listings_by_seller(self.phone)
        return {
            "listings": [
                {"id": l["id"], "title": l["title"], "price": self._money(l["price_cents"]),
                 "quantity": l["quantity"], "status": l["status"]}
                for l in listings
            ]
        }

    def _t_update_listing(
        self,
        listing_id: int,
        price: float | None = None,
        quantity: int | None = None,
        description: str | None = None,
        remove: bool = False,
    ) -> dict[str, Any]:
        listing = self.store.update_listing(
            self.phone, listing_id,
            price_cents=_cents(price) if price is not None else None,
            quantity=quantity, description=description, remove=remove,
        )
        return {"updated": self._public_listing(listing)}

    def _t_place_order(self, listing_id: int, quantity: int, note: str = "") -> dict[str, Any]:
        order = self.store.place_order(self.phone, listing_id, quantity, note)
        msg = (
            f"🛒 *New order #{order['id']}*\n"
            f"{order['buyer_name'] or 'A buyer'} wants {order['quantity']} × {order['listing_title']} "
            f"({self._money(order['total_cents'])})."
        )
        if note:
            msg += f"\nNote: {note}"
        msg += "\nReply here to accept or decline it."
        self.notify(order["seller_phone"], msg)
        return {"order": self._order_view(order), "seller_notified": True}

    def _t_my_orders(self) -> dict[str, Any]:
        return {"orders": [self._order_view(o) for o in self.store.orders_for(self.phone)]}

    def _t_update_order_status(self, order_id: int, status: str) -> dict[str, Any]:
        order = self.store.update_order_status(self.phone, order_id, status)
        other = order["buyer_phone"] if order["seller_phone"] == self.phone else order["seller_phone"]
        msg = f"📦 Order #{order['id']} ({order['listing_title']}) is now *{status}*."
        if status == "accepted":
            msg += f"\nContact the seller on WhatsApp: +{order['seller_phone']}"
        self.notify(other, msg)
        return {"order": self._order_view(order), "other_party_notified": True}
