# Footwear Finder

At many temples there's no footwear counter: people leave their footwear wherever they can, then
can't find it after darshan. They wander back against the incoming crowd searching, and that
counter-flow is one of the things that makes crowds dangerous. Footwear Finder is a WhatsApp bot that
remembers where each person left theirs and guides them back the right way.

```
 Board "E1" with QR  ──scan──►  WhatsApp opens with "SAVE E1"  ──send──►  ✅ Saved: East gate, left of the steps
                                (optional) photo of footwear    ──send──►  📸 Photo saved
 after darshan:                 "where?" / anything             ──send──►  👟 E1: East gate, left of the steps
                                                                            🚶 Leave by Exit 2, turn right...
                                                                            [their photo]
                                "FOUND"                         ──send──►  🙏 deleted
```

- **No app to install, works on any phone** with WhatsApp and a camera. People without a QR scanner
  type `SAVE E1`.
- **Routes avoid counter-flow.** Each spot has a route that uses the exits, so people don't push back
  in against the queue.
- **The photo** helps people pick out their own pair from a heap.
- **Privacy:** nothing is kept after "FOUND", and everything (phone number, photo) is deleted after
  `retention_hours` (12 by default).
- **No WhatsApp template needed:** the devotee always messages first, so replies are free-form.
- **Staff view:** `GET /api/spots` (password-protected) shows how many people have footwear saved
  at each spot, which tells you where to put boards, volunteers and cleaners.
- **Languages:** replies are English; "found" is also understood in Hindi, Tamil, Telugu, Kannada and
  Malayalam. The boards carry English and Hindi. Have a native speaker check the wording before
  printing, and add your local language.

Pair it with **CrowdWatch** (`../crowd-safety`), which alerts staff when footwear piles up on steps
and in exits where people could trip.

## Setup

```bash
cd footwear-finder
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp finder.example.yaml finder.yaml      # set your spots, routes and WhatsApp number
```

1. **Spots:** walk the temple and list every place where footwear gets left. Give each a short code
   (E1, E2, N1…), a name people will recognise, and a route back that uses the exits.
2. **Boards:** `python -m finder.posters finder.yaml posters.html`, open the file in a browser, print
   on A4 and laminate. Mount boards at eye level, visible from where people take off their footwear.
3. **WhatsApp:** in Meta for Developers, set up the WhatsApp Cloud API for the temple's number, then:
   ```bash
   export WHATSAPP_ACCESS_TOKEN=...  WHATSAPP_PHONE_NUMBER_ID=...
   export WHATSAPP_VERIFY_TOKEN=any-string  WHATSAPP_APP_SECRET=...
   export FINDER_STATS_PASSWORD=...          # for /api/spots
   FINDER_CONFIG=finder.yaml uvicorn finder.app:app --host 0.0.0.0 --port 8000
   ```
   Point the webhook to `https://<your-host>/webhook`, subscribed to `messages`.
4. **Test it:** scan a board, send a photo, message "where", then "found".

## Tests

```bash
python -m pytest
```

They cover the full conversation (save → photo → where → found, photo first, code in caption,
next-day visits, unknown codes, codes inside longer words), retention purge, webhook signature,
verification and redelivery dedupe, the staff stats endpoint, and poster QR links.
