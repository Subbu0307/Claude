# WhatsApp Marketplace Agent

A peer-to-peer marketplace that people use entirely through WhatsApp. Buyers and sellers chat with
an AI agent (Claude) that searches listings, publishes items, places orders, and messages the
other party, all inside the chat.

```
 WhatsApp user ──► Meta WhatsApp Cloud API ──► POST /webhook (FastAPI)
                                                    │
                                                    ▼
                                    MarketplaceAgent (Claude + tools)
                                     │                         │
                                     ▼                         ▼
                              SQLite store            WhatsApp send API
                   (users, listings, orders,     (replies + notifications
                    per-user chat history)          to buyers/sellers)
```

## What users can do

| Buyer says… | Agent does |
|---|---|
| "Looking for a used bike under 100" | `search_listings` → short list with `#id`, price, location |
| "Tell me more about #12" | `get_listing` |
| "I'll take it" | Confirms item, quantity and total, then `place_order` (reserves stock and pings the seller) |
| "Where's my order?" | `my_orders` |
| "Got it, thanks!" | `update_order_status → completed` |

| Seller says… | Agent does |
|---|---|
| "I want to sell my iPhone 13" | Asks for anything missing (price, condition…), confirms, `create_listing` |
| "Drop the price of #12 to 80" | `update_listing` |
| "Accept order #5" / "Shipped #5" | `update_order_status` (buyer is notified) |

Order lifecycle: `pending → accepted → shipped → completed`. Either side can cancel early
(seller: pending/accepted; buyer: pending), and cancelling returns the stock to the listing.
Buyer and seller get each other's WhatsApp number once the seller accepts. Payment and handover
happen directly between them; the marketplace never handles money.

Send `reset` to start a fresh conversation. History also resets after 24h of inactivity.

## Design notes

- **Agent loop**: `marketplace/agent.py` runs a manual tool-use loop on `claude-opus-5-5` with
  strict tool schemas, effort `low` by default (chat traffic; raise `MARKETPLACE_EFFORT` if needed),
  prompt caching, and server-side refusal fallbacks (`fallbacks: "default"`).
- **Security**: every tool runs as the WhatsApp sender. The phone number is bound server-side in
  `ToolExecutor`, never taken from model output, so a user can't edit someone else's listing or
  read their orders. Ownership and stock rules are enforced in `store.py` inside transactions.
- **Webhook**: verifies `X-Hub-Signature-256` with your app secret, acknowledges at once and
  processes in the background, deduplicates WhatsApp redeliveries, and serialises turns per user.
- **History**: stored per user, append-only, with Claude's content blocks echoed back unchanged.
  If a turn is interrupted after a tool already ran (for example, an order was placed), that record
  is kept so the next turn knows about it.

## Run locally (no WhatsApp needed)

```bash
cd whatsapp-marketplace
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

# Terminal 1: seller
python -m marketplace.cli --phone 15550001111 --name Sam
# Terminal 2: buyer
python -m marketplace.cli --phone 15550002222 --name Priya
```

Both terminals share `marketplace.db`. Messages the agent would send to the other user over
WhatsApp are printed to the console.

## Connect to WhatsApp

1. In [Meta for Developers](https://developers.facebook.com/), create an app, add the **WhatsApp**
   product, and note the **Phone number ID** and a **permanent access token** (system user).
2. `cp .env.example .env` and fill it in, then load it (`set -a; . ./.env; set +a`).
3. Start the server and expose it over HTTPS (for example with `ngrok http 8000`):
   ```bash
   uvicorn marketplace.app:app --host 0.0.0.0 --port 8000
   ```
4. In the WhatsApp **Configuration** page, set the callback URL to `https://<your-host>/webhook`
   and the verify token to your `WHATSAPP_VERIFY_TOKEN`, then subscribe to the `messages` field.
5. Message your business number from WhatsApp.

> WhatsApp only allows free-form messages within 24h of a user's last message to you. A seller who
> hasn't chatted recently won't receive order notifications until you add an approved **template
> message** for them. That's the main production gap to close.

## Tests

```bash
pip install -r requirements.txt
python -m pytest
```

The tests use a scripted fake Claude client, so they need no API key.

## Ideas for next steps

- Template messages for out-of-window notifications
- Photos on listings (WhatsApp image messages → media download → stored URL)
- Interactive buttons/lists for confirming orders
- Postgres instead of SQLite for multi-instance deployments
- Ratings and reviews after `completed` orders
