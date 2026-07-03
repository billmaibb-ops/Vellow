# Vellow — automated dropshipping storefront

A zero-inventory storefront that lists CJ Dropshipping products with a
risk-adjusted markup, verifies stock in real time before charging the
customer, and forwards paid orders to CJ for fulfillment.

## How the pieces fit together

```
                 ┌─────────────────────┐
   every hour →  │  sync_engine.py      │  polls CJ for price + stock
   every day  →  │  (--mode hourly/daily)│  writes ↓
                 └─────────────────────┘
                            │
                     products.json  ◄── single source of truth (prices, stock)
                            │
                 ┌─────────────────────┐
   customer   →  │  index.html          │  reads products.json via fetch()
   browses/buys  │  (storefront)        │  calls ↓ on "Place order"
                 └─────────────────────┘
                            │
                 ┌─────────────────────┐
                 │  server.py           │  1. create-hold  (Stripe auth only)
                 │  (order backend)     │  2. verify-and-capture:
                 └─────────────────────┘        re-check CJ stock →
                            │                    capture funds → send to CJ
                       Stripe + CJ
```

The golden rule enforced everywhere: **money is only captured after stock
is confirmed.** Any failure releases the authorization hold instead of
charging the customer.

## Files

| File | What it does |
|---|---|
| `index.html` | The storefront (single file, Tailwind). Reads `products.json`. |
| `catalog.html` | **CJ catalog browser** — search/browse the *entire* CJ catalog (paginated, live via the backend), with your retail markup shown and an "Add to my store" button that appends to `watchlist.json`. Falls back to demo data if the backend is offline. |
| `products.json` | Live catalog: prices, stock, store config. Written by the sync engine. |
| `backend/pricing.py` | The risk-adjusted price formula. Single source of truth. |
| `backend/generate_catalog.py` | Seeds `products.json` with 500+ demo products across 12 categories (priced with the real formula). Real synced items are preserved; `--prune-demo` removes placeholders once you have real CJ data. |
| `backend/cj_client.py` | CJ Dropshipping API v2 client (auth, products, stock, orders). |
| `backend/sync_engine.py` | Hourly price/stock poll + daily deep sync → `products.json`. |
| `backend/watchlist.json` | The CJ product IDs you've chosen to sell. |
| `backend/server.py` | Order backend: Stripe auth-hold, verify-and-capture, CJ order forward. |

## Pricing model (why you don't lose money in aggregate)

```
retail = max(
    cost * (1 + profit_target + loss_provision_rate) / (1 - gateway_fee_rate),
    (cost + min_profit_per_unit + chargeback_rate * chargeback_fee) / (1 - gateway_fee_rate)
)
```

All knobs live in `products.json → store`. The absolute floor
(`min_profit_per_unit`, default $7) is what protects cheap items — a $1.20
item still sells for ~$8.66, giving a ~$7 cushion that covers returns and
chargebacks across your order volume. This makes the *store* profitable;
it cannot make any *single* chargeback impossible to lose on. Keep losses
down with Stripe Radar (fraud screening) and fast, tracked shipping.

## Setup

```bash
cd backend
python -m venv venv && source venv/bin/activate     # optional
pip install -r requirements.txt
cp .env.example .env                                 # then edit .env
export $(grep -v '^#' .env | xargs)                  # load env vars
```

1. **CJ API key** — CJ dashboard → Account → API. Paste into `.env`.
2. **Pick products** — search CJ (`python -c "from cj_client import CJClient; print(CJClient().search_products('phone holder'))"`),
   put the `pid` (and variant `vid`) into `watchlist.json`. Favor items
   showing **US-warehouse** stock for 3–8 day shipping.
3. **First catalog build**:
   ```bash
   python sync_engine.py --mode daily
   ```
   This deep-syncs images/descriptions/prices into `../products.json`.
4. **Stripe** — put your `sk_test_` key in `.env`. In the Stripe dashboard,
   nothing special is needed; the code sets `capture_method="manual"` so
   holds aren't captured automatically.
5. **Run the order backend**:
   ```bash
   python server.py        # http://localhost:8000
   ```
6. **Serve the storefront** (any static server), e.g.:
   ```bash
   cd .. && python -m http.server 5500     # http://localhost:5500
   ```

## Browsing the full CJ catalog (`catalog.html`)

CJ's catalog is millions of SKUs, so it can't be dumped statically — the
browser pages through it live via the backend:

- `GET /api/catalog?page=&size=&q=&category=&us=1` — proxies CJ `listV2`,
  applies the risk-adjusted retail price server-side, caches 5 min per page.
- `GET /api/catalog/categories` — CJ category tree, cached 24 h.
- `POST /api/watchlist/add {pid,title,category}` — adds an item to
  `watchlist.json`. Run `sync_engine.py --mode daily` afterward to deep-sync
  it (variant id, images, real price) into `products.json` / the storefront.

Workflow: browse `catalog.html` → add winners to your store → daily sync
publishes them. The storefront still only sells watchlisted, synced items —
the golden capture-after-verify rule is unchanged.

## Scheduling the sync (run once per hour, per spec)

Use cron (mac/linux) — do **not** run an infinite loop:

```cron
0  * * * *  cd /path/to/backend && /path/to/venv/bin/python sync_engine.py --mode hourly
30 3 * * *  cd /path/to/backend && /path/to/venv/bin/python sync_engine.py --mode daily
```

## Before you go live — the honest checklist

- **Fraud screening on.** Enable Stripe Radar. Chargebacks are the #1 way
  this model loses money; the margin only survives if the chargeback rate
  stays low.
- **Shipping speed.** Prefer CJ US-warehouse SKUs. Slow shipping → refunds
  and "item not received" chargebacks that eat the cushion.
- **Legal / consumer protection.** The checkout promises ship-within-5-days
  or a full refund (FTC Mail Order Rule). Honor it. Have a real returns
  policy, terms, and privacy page.
- **Sales tax.** Once you cross economic nexus thresholds you must collect
  and remit. Use a tool (Stripe Tax / TaxJar) rather than guessing.
- **Business registration & 1099-K.** Stripe reports your revenue. Register
  the business and keep records.
- **Product/IP.** Only list CJ catalog items you have the right to sell;
  use CJ's provided images and copy, not scraped marketplace listings.

None of this is legal or tax advice — I'm not a lawyer or accountant.
Confirm the tax and consumer-protection obligations for where you operate.
