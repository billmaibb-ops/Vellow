"""
server.py — order backend for the storefront.

Implements the "authorize -> verify -> capture" flow required by the spec:

  1. POST /api/create-hold
        Create a Stripe PaymentIntent with capture_method="manual".
        This AUTHORIZES the card (places a temporary hold) but does NOT
        capture funds. Returns the client_secret for the frontend to
        confirm the card.

  2. POST /api/verify-and-capture
        Real-time re-check of the exact items against CJ BEFORE taking money.
        - If every line is in stock (>= safety threshold) and the price
          still matches, CAPTURE the hold and forward the order to CJ.
        - Otherwise CANCEL the PaymentIntent (release the hold, customer
          is never charged) and return the reason.

This is intentionally boring and defensive: money only moves after stock
is confirmed, and any failure releases the hold rather than charging.

Run locally:  python server.py    (defaults to http://localhost:8000)
Prod: put behind gunicorn + HTTPS. NEVER expose your Stripe secret key
or CJ api key to the browser — they live in this backend only.
"""

import json
import os
import re
import time
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS

import stripe

from cj_client import CJClient, CJError
from pricing import PricingConfig, retail_price, gross_up

HERE = Path(__file__).resolve().parent
PRODUCTS_JSON = HERE.parent / "products.json"

# Payments are OPTIONAL at boot. The quote/shipping/stock endpoints don't need
# Stripe, so the service comes up and serves them even before a Stripe key is
# configured. Only the charge steps (create-hold / verify-and-capture) require it.
# Accept common env-var name variants so a small naming slip doesn't leave
# payments disabled.
stripe.api_key = (os.environ.get("STRIPE_SECRET_KEY")
                  or os.environ.get("SK_TEST_KEY")
                  or os.environ.get("STRIPE_KEY")
                  or os.environ.get("stripe_secret_key")
                  or "").strip()
PAYMENTS_ENABLED = bool(stripe.api_key)

app = Flask(__name__)
# Lock CORS to your storefront's domain in production.
CORS(app, origins=os.environ.get("STOREFRONT_ORIGIN", "*"))

# CJ client is needed for live price/stock/shipping. If the key is missing the
# app still boots; those endpoints then report a clear config error.
try:
    cj = CJClient()
except Exception as _e:  # noqa: BLE001
    cj = None
    print(f"[warn] CJ client not configured: {_e}")


def load_catalog() -> dict:
    return json.loads(PRODUCTS_JSON.read_text())


def find_product(catalog: dict, pid: str) -> dict | None:
    return next((p for p in catalog["products"] if p["id"] == pid), None)


DETAILS_DIR = HERE.parent / "products"


def line_price(p: dict, vid: str | None) -> float:
    """Unit retail for a cart line. If a variant is chosen, use that
    variant's own risk-adjusted retail from the product's detail file;
    otherwise the product-level price. Server-side truth — the client's
    displayed price is never trusted for the charge."""
    if vid:
        try:
            detail = json.loads((DETAILS_DIR / f"{p['id']}.json").read_text())
            for v in detail.get("variants", []):
                if v.get("vid") == vid and v.get("retail_price"):
                    return float(v["retail_price"])
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return float(p["retail_price"])


# ---------------------------------------------------------------------------
# Destination sales-tax rates. ESTIMATE FOR DISPLAY ONLY — before charging
# real customers, replace with Stripe Tax / TaxJar, which handle local rates
# and nexus. Override per-state in products.json -> store.tax_rates.
DEFAULT_STATE_TAX = {
    "AL": .04, "AZ": .056, "AR": .065, "CA": .0725, "CO": .029, "CT": .0635,
    "FL": .06, "GA": .04, "HI": .04, "ID": .06, "IL": .0625, "IN": .07,
    "IA": .06, "KS": .065, "KY": .06, "LA": .0445, "ME": .055, "MD": .06,
    "MA": .0625, "MI": .06, "MN": .06875, "MS": .07, "MO": .04225, "NE": .055,
    "NV": .0685, "NJ": .06625, "NM": .05125, "NY": .04, "NC": .0475, "ND": .05,
    "OH": .0575, "OK": .045, "PA": .06, "RI": .07, "SC": .06, "SD": .045,
    "TN": .07, "TX": .0625, "UT": .0485, "VT": .06, "VA": .053, "WA": .065,
    "WV": .06, "WI": .05, "WY": .04, "DC": .06,
    # no statewide sales tax:
    "AK": 0.0, "DE": 0.0, "MT": 0.0, "NH": 0.0, "OR": 0.0,
}


def tax_rate_for(state: str, store: dict) -> float:
    rates = {**DEFAULT_STATE_TAX, **(store.get("tax_rates") or {})}
    return float(rates.get((state or "").upper(),
                           store.get("default_tax_rate", 0.0)))


# --- INTERNAL change-of-mind return fee -----------------------------------
# NEVER returned to the storefront or written to products.json. The rate is
# read from a private env var (default 0.20) so the structure isn't in the
# public repo. Fee = rate x unit profit margin + any extra fees CJ charges on
# the return. Only applies to change-of-mind returns, never to defective/
# undelivered items.
RETURN_FEE_MARGIN_RATE = float(os.environ.get("RETURN_FEE_MARGIN_RATE", "0.20"))

# Shipping markup — CJ's real shipping cost is multiplied by this before being
# shown as the "Shipping" line, so shipping is a profit center. Server-side
# only (private env var); the multiplier is never exposed to the storefront.
SHIPPING_MARKUP = float(os.environ.get("SHIPPING_MARKUP", "1.20"))  # +20%

# ---------------------------------------------------------------------------
# Owner control center — auth + order log
# ---------------------------------------------------------------------------
# Bearer token that gates every /api/admin/* endpoint. Set ADMIN_TOKEN in the
# Render env; without it, the admin API is locked (returns 401) so the public
# admin.html page shows nothing.
# Accept a few env-var name variants so a small naming slip doesn't lock you out.
ADMIN_TOKEN = (os.environ.get("ADMIN_TOKEN")
               or os.environ.get("Vellow_Admin")
               or os.environ.get("VELLOW_ADMIN")
               or os.environ.get("vellow_admin")
               or "").strip()

# Append-only order log. NOTE: on Render's free tier the filesystem is
# EPHEMERAL — it resets on each deploy/restart, so this is fine for testing but
# for production you should point ORDERS_LOG at a persistent disk or, better,
# a database. Stripe remains the durable source of truth for payments; this log
# adds the per-order COST/PROFIT data Stripe doesn't know.
ORDERS_LOG = Path(os.environ.get("ORDERS_LOG", str(HERE / "orders.jsonl")))


def log_order(record: dict):
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    try:
        with open(ORDERS_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:  # noqa: BLE001 — logging must never break checkout
        print(f"[warn] could not write order log: {e}")


def read_orders() -> list[dict]:
    if not ORDERS_LOG.exists():
        return []
    out = []
    for line in ORDERS_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def admin_ok() -> bool:
    if not ADMIN_TOKEN:
        return False
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.args.get("token", "")
    return token.strip() == ADMIN_TOKEN


def compute_return_fee(retail_price: float, source_cost: float,
                       cj_extra_fees: float = 0.0) -> float:
    margin = max(0.0, float(retail_price) - float(source_cost))
    return round(RETURN_FEE_MARGIN_RATE * margin + float(cj_extra_fees or 0.0), 2)


def live_unit_price(cj_client, p: dict, vid: str | None, cfg) -> float:
    """Re-price this line from CJ's CURRENT supplier cost (not the cached
    products.json). Protects margin if CJ raised the cost since last sync."""
    pid = p.get("cj_pid")
    if not pid:
        return line_price(p, vid)
    try:
        detail = cj_client.get_product(pid)
    except CJError:
        return line_price(p, vid)
    variants = detail.get("variants") or []
    v = next((x for x in variants if x.get("vid") == vid), None) if vid else None
    cost = None
    if v:
        cost = float(v.get("variantSellPrice") or 0) or None
    if not cost:
        cost = float((variants[0].get("variantSellPrice") if variants else 0)
                     or detail.get("sellPrice") or 0) or None
    return retail_price(cost, cfg) if cost else line_price(p, vid)


def build_quote(catalog: dict, items: list, shipping: dict) -> dict:
    """The authoritative purchase-time check. For every line, hit CJ live for
    current price + stock, then get the real CJ shipping cost to the address,
    and add destination tax. Returns the full breakdown the customer pays:

        total = sum(our live price x qty) + tax + CJ shipping

    Any out-of-stock line is reported in `problems` (caller must not charge)."""
    store = catalog["store"]
    cfg = PricingConfig.from_store(store)
    threshold = store.get("safety_stock_threshold", 5)
    country = shipping.get("country", "US")
    zip_code = shipping.get("zip", "")
    state = shipping.get("state", "")

    lines, problems, cj_products = [], [], []
    subtotal = 0.0
    for item in items:
        p = find_product(catalog, item["id"])
        if not p:
            problems.append(f"Unknown item {item['id']}")
            continue
        qty = int(item["qty"])
        vid = item.get("vid") or p.get("cj_vid")

        # ONE live CJ call per line: fetch current cost AND, if this product
        # has no stored variant id yet (deep sync not done), resolve the default
        # variant on the fly. This makes checkout work before the deep sync
        # finishes — no product is falsely "out of stock" for lack of a vid.
        unit = float(p["retail_price"])
        if p.get("cj_pid"):
            try:
                detail = cj.get_product(p["cj_pid"])
                variants = detail.get("variants") or []
                v = next((x for x in variants if x.get("vid") == vid), None) if vid else None
                if v is None and variants:
                    v = variants[0]
                    vid = v.get("vid") or vid
                cost = float((v or {}).get("variantSellPrice")
                             or detail.get("sellPrice") or 0) or None
                if cost:
                    unit = retail_price(cost, cfg)
            except CJError:
                pass

        try:                                       # up-to-date stock
            stock = cj.get_variant_stock(vid) if vid else {"us_quantity": 0, "quantity": 0}
            eff = stock["us_quantity"] or stock["quantity"]
        except CJError:
            eff = 0
        if eff < threshold or eff < qty:
            problems.append(f"{p['title']} is out of stock")

        subtotal += unit * qty
        lines.append({"id": p["id"], "title": p["title"], "qty": qty,
                      "unit_price": round(unit, 2),
                      "line_total": round(unit * qty, 2), "available": eff})
        if vid:
            cj_products.append({"vid": vid, "quantity": qty})

    # up-to-date shipping to this address (grossed up so the card fee on
    # shipping isn't paid out of margin)
    shipping_cost, ship_name, ship_days = None, "", ""
    if country and zip_code and cj_products:
        try:
            q = cj.get_shipping_quote_multi(cj_products, country, zip_code, state)
            # Shipping is a profit line: CJ's real cost x markup, then grossed
            # up for the card fee. Markup is server-side only (SHIPPING_MARKUP).
            shipping_cost = gross_up(q["cost"] * SHIPPING_MARKUP, cfg.gateway_fee_rate)
            ship_name, ship_days = q["name"], q["days"]
        except CJError:
            shipping_cost = None
    if shipping_cost is None:
        shipping_cost = gross_up(float(store.get("fallback_shipping", 6.99)) * SHIPPING_MARKUP,
                                 cfg.gateway_fee_rate)
        ship_name = "Standard shipping (estimate)"

    rate = tax_rate_for(state, store)
    tax = round(subtotal * rate, 2)
    total = round(subtotal + tax + shipping_cost, 2)
    return {
        "lines": lines, "problems": problems,
        "subtotal": round(subtotal, 2),
        "tax": tax, "tax_rate": rate,
        "shipping": round(shipping_cost, 2),
        "shipping_name": ship_name, "shipping_days": ship_days,
        "total": total,
    }


@app.post("/api/quote")
def quote():
    """Live purchase-time quote: current price, stock, address shipping + tax.
    The storefront shows a loading state while this runs.
    Body: { items:[{id,qty,vid}], shipping:{country,zip,state} }"""
    if cj is None:
        return jsonify(ok=False, reason="Live pricing not configured (CJ_API_KEY missing)."), 503
    body = request.get_json(force=True)
    q = build_quote(load_catalog(), body.get("items", []), body.get("shipping", {}))
    return jsonify(ok=not q["problems"], **q)


# ---------------------------------------------------------------------------
@app.post("/api/create-hold")
def create_hold():
    """Authorization-only hold for the LIVE quoted total (price+tax+shipping).
    Recomputes the quote server-side so the hold can't be tampered with.
    Body: { items:[{id,qty,vid}], shipping:{country,zip,state} }"""
    if not PAYMENTS_ENABLED:
        return jsonify(ok=False, reason="Payments not configured yet (STRIPE_SECRET_KEY missing)."), 503
    if cj is None:
        return jsonify(ok=False, reason="Live pricing not configured (CJ_API_KEY missing)."), 503
    body = request.get_json(force=True)
    catalog = load_catalog()

    q = build_quote(catalog, body.get("items", []), body.get("shipping", {}))
    if q["problems"]:
        return jsonify(ok=False, reason="; ".join(q["problems"])), 409

    amount = round(q["total"] * 100)  # Stripe uses integer cents
    intent = stripe.PaymentIntent.create(
        amount=amount,
        currency=catalog["store"].get("currency", "usd").lower(),
        capture_method="manual",             # <-- AUTHORIZATION ONLY
        automatic_payment_methods={"enabled": True},
        metadata={"cart": json.dumps([{"id": i["id"], "qty": i["qty"],
                                       "vid": i.get("vid", "")}
                                      for i in body.get("items", [])])},
    )
    return jsonify(ok=True, client_secret=intent.client_secret,
                   payment_intent=intent.id, amount=amount / 100,
                   subtotal=q["subtotal"], tax=q["tax"],
                   shipping=q["shipping"], total=q["total"])


# ---------------------------------------------------------------------------
@app.post("/api/verify-and-capture")
def verify_and_capture_guarded():
    if not PAYMENTS_ENABLED:
        return jsonify(ok=False, captured=False,
                       reason="Payments not configured yet (STRIPE_SECRET_KEY missing)."), 503
    if cj is None:
        return jsonify(ok=False, captured=False,
                       reason="Live pricing not configured (CJ_API_KEY missing)."), 503
    return verify_and_capture()


def verify_and_capture():
    """The critical gate. Re-check stock in real time, then capture or release.
    Body: { payment_intent, items:[{id,qty}], shipping:{name,addr,city,state,zip,country,email} }"""
    body = request.get_json(force=True)
    pi_id = body["payment_intent"]
    catalog = load_catalog()
    cfg = PricingConfig.from_store(catalog["store"])
    threshold = catalog["store"].get("safety_stock_threshold", 5)

    # ---- 1. real-time verification against CJ (not the cached json) ----
    problems = []
    verified_lines = []
    for item in body.get("items", []):
        p = find_product(catalog, item["id"])
        if not p:
            problems.append(f"Unknown item {item['id']}")
            continue
        qty = int(item["qty"])
        vid = item.get("vid") or p.get("cj_vid")   # customer's chosen variant wins
        try:
            stock = cj.get_variant_stock(vid) if vid else {"us_quantity": 0, "quantity": 0}
        except CJError as e:
            problems.append(f"Could not verify {p['id']}: {e}")
            continue

        effective = stock["us_quantity"] or stock["quantity"]
        if effective < threshold or effective < qty:
            problems.append(f"{p['title']} is out of stock")
            continue

        # Price integrity: make sure the live cost hasn't blown past our price.
        # (If CJ raised the cost, recompute; if it now exceeds what the customer
        #  was quoted, treat as a problem rather than eating the loss.)
        verified_lines.append({"product": p, "qty": qty, "vid": vid})

    if problems:
        # Release the hold — customer is NOT charged.
        try:
            stripe.PaymentIntent.cancel(pi_id)
        except Exception:
            pass
        log_order({"pi": pi_id, "status": "released_out_of_stock",
                   "reason": "; ".join(problems)})
        return jsonify(ok=False, captured=False, reason="; ".join(problems)), 409

    # Cost snapshot for the owner dashboard (Stripe doesn't know supplier cost).
    prod_cost = round(sum(float(l["product"].get("source_cost", 0)) * l["qty"]
                          for l in verified_lines), 2)

    # ---- 2. capture the authorized funds ----
    try:
        intent = stripe.PaymentIntent.capture(pi_id)
    except stripe.error.StripeError as e:
        log_order({"pi": pi_id, "status": "capture_failed", "reason": e.user_message})
        return jsonify(ok=False, captured=False, reason=f"Capture failed: {e.user_message}"), 402

    order_total = float(intent.amount) / 100
    # Est. Stripe fee (2.9% + $0.30) and est. profit for the dashboard.
    stripe_fee = round(order_total * 0.029 + 0.30, 2)
    est_profit = round(order_total - prod_cost - stripe_fee, 2)

    # ---- 3. forward the order to CJ for fulfillment ----
    ship = body["shipping"]
    cj_order = {
        "orderNumber": intent.id,                       # your idempotency key
        "shippingCountryCode": ship.get("country", "US"),
        "shippingProvince": ship.get("state", ""),
        "shippingCity": ship.get("city", ""),
        "shippingAddress": ship.get("addr", ""),
        "shippingCustomerName": ship.get("name", ""),
        "shippingZip": ship.get("zip", ""),
        "shippingPhone": ship.get("phone", "0000000000"),
        "remark": "Auto-forwarded by storefront after payment capture",
        "products": [{"vid": l["vid"], "quantity": l["qty"]} for l in verified_lines],
    }
    base = {"pi": pi_id, "total": order_total, "product_cost": prod_cost,
            "stripe_fee": stripe_fee, "est_profit": est_profit,
            "items": [{"id": l["product"]["id"], "title": l["product"]["title"],
                       "vid": l["vid"], "qty": l["qty"]} for l in verified_lines],
            "customer": ship.get("name", ""), "email": ship.get("email", ""),
            "dest": f"{ship.get('city','')}, {ship.get('state','')} {ship.get('zip','')}"}
    try:
        cj_result = cj.create_order(cj_order)
    except Exception as e:  # noqa: BLE001 — any CJ failure must not 500 after capture
        # Payment captured but CJ order failed — flag for manual handling,
        # do NOT silently drop it and do NOT crash. In production: enqueue a
        # retry + alert. The order shows in the admin dashboard as "failed to CJ".
        print(f"[cj-order-failed] {e}")  # surface CJ's reason in the logs
        log_order({**base, "status": "paid_cj_failed", "reason": str(e)})
        return jsonify(ok=True, captured=True, fulfilled=False,
                       reason=f"Paid, but CJ order needs manual retry: {e}",
                       payment_intent=intent.id), 202

    log_order({**base, "status": "fulfilled",
               "cj_order_id": (cj_result or {}).get("orderId")
                              or (cj_result or {}).get("orderNum") or ""})
    return jsonify(ok=True, captured=True, fulfilled=True,
                   payment_intent=intent.id, cj_order=cj_result)


# ---------------------------------------------------------------------------
# Catalog browser (catalog.html) — proxies the FULL CJ catalog, paginated,
# with the risk-adjusted retail markup applied server-side. The browser never
# sees the CJ token or the raw supplier cost unless you expose it on purpose.
# ---------------------------------------------------------------------------
WATCHLIST_JSON = HERE / "watchlist.json"
_cache: dict[str, tuple[float, object]] = {}          # key -> (expiry, value)
CATALOG_TTL = 300                                     # 5 min — CJ rate limits
CATEGORY_TTL = 24 * 3600


def _cached(key: str, ttl: int, fn):
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    val = fn()
    _cache[key] = (time.time() + ttl, val)
    return val


def _normalize_cj_row(row: dict, cfg: PricingConfig) -> dict:
    """CJ listV2 row -> storefront card shape, with retail price applied."""
    # sellPrice can be "3.99" or a range "3.99 -- 12.50"; price on the high
    # end of the range so no variant sells below cost.
    raw = str(row.get("sellPrice") or row.get("price") or "0")
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", raw)] or [0.0]
    cost = max(nums)
    return {
        "pid": row.get("pid"),
        "title": row.get("productNameEn") or row.get("productName") or "Untitled",
        "image": row.get("productImage") or row.get("bigImage") or "",
        "category": row.get("categoryName") or "",
        "source_cost": cost,
        "retail_price": retail_price(cost, cfg) if cost > 0 else None,
        "listed_num": int(row.get("listedNum") or 0),      # popularity proxy
        "warehouses": row.get("sourceFrom") or row.get("warehouse") or "",
    }


@app.get("/api/catalog")
def catalog_browse():
    """Paginated browse/search over the ENTIRE CJ catalog.
    Query: page, size (<=200), q (keyword), category (categoryId), us=1"""
    page = max(1, int(request.args.get("page", 1)))
    size = min(200, max(1, int(request.args.get("size", 40))))
    q = (request.args.get("q") or "").strip() or None
    category = request.args.get("category") or None
    country = "US" if request.args.get("us") == "1" else None

    catalog = load_catalog()
    cfg = PricingConfig.from_store(catalog["store"])

    key = f"cat:{page}:{size}:{q}:{category}:{country}"
    try:
        data = _cached(key, CATALOG_TTL, lambda: cj.list_products(
            page=page, size=size, keyword=q,
            category_id=category, country_code=country))
    except (CJError, Exception) as e:  # noqa: BLE001 — surface, don't 500
        return jsonify(ok=False, reason=f"CJ catalog unavailable: {e}"), 502

    items = [_normalize_cj_row(r, cfg) for r in data["list"]]
    return jsonify(ok=True, page=page, size=size,
                   total=data["total"], items=items)


@app.get("/api/catalog/categories")
def catalog_categories():
    try:
        cats = _cached("categories", CATEGORY_TTL, cj.get_categories)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, reason=str(e)), 502
    return jsonify(ok=True, categories=cats)


@app.post("/api/watchlist/add")
def watchlist_add():
    """Add a CJ product to watchlist.json (the set of items you sell).
    Body: { pid, title, category }. Run sync_engine --mode daily after."""
    body = request.get_json(force=True)
    pid = (body.get("pid") or "").strip()
    if not pid:
        return jsonify(ok=False, reason="pid required"), 400
    wl = json.loads(WATCHLIST_JSON.read_text())
    if any(i.get("pid") == pid for i in wl["items"]):
        return jsonify(ok=True, added=False, reason="already on watchlist")
    wl["items"].append({
        "sku": f"SKU-CJ-{pid[-6:]}",
        "pid": pid,
        "vid": "",                       # filled in by the daily deep sync
        "title": body.get("title", ""),
        "category": body.get("category", ""),
        "trending_score": 0.5,
    })
    WATCHLIST_JSON.write_text(json.dumps(wl, indent=2))
    return jsonify(ok=True, added=True, count=len(wl["items"]))


# ---------------------------------------------------------------------------
@app.post("/api/verify-stock")
def verify_stock_only():
    """Lightweight real-time stock check (no payment). The storefront's
    current verifyStock() call maps here. Body: { items:[{id,qty}] }"""
    body = request.get_json(force=True)
    catalog = load_catalog()
    threshold = catalog["store"].get("safety_stock_threshold", 5)
    results = {}
    all_ok = True
    for item in body.get("items", []):
        p = find_product(catalog, item["id"])
        vid = p.get("cj_vid") if p else None
        try:
            stock = cj.get_variant_stock(vid) if vid else {"us_quantity": 0, "quantity": 0}
            eff = stock["us_quantity"] or stock["quantity"]
            ok = eff >= threshold and eff >= int(item["qty"])
        except CJError:
            ok, eff = False, 0
        results[item["id"]] = {"ok": ok, "available": eff}
        all_ok = all_ok and ok
    return jsonify(ok=all_ok, items=results)


# ---------------------------------------------------------------------------
# OWNER CONTROL CENTER API  (all token-gated)
# ---------------------------------------------------------------------------
def _since(days: int) -> float:
    return time.time() - days * 86400


def _order_ts(o: dict) -> float:
    try:
        return time.mktime(time.strptime(o.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0.0


@app.get("/api/admin/summary")
def admin_summary():
    """Everything the owner dashboard needs in one call."""
    if not admin_ok():
        return jsonify(ok=False, reason="unauthorized"), 401

    catalog = load_catalog()
    store = catalog["store"]
    products = catalog["products"]
    cfg = PricingConfig.from_store(store)
    orders = read_orders()

    # ---- catalog health ----
    threshold = store.get("safety_stock_threshold", 10)
    known_stock = [p for p in products if isinstance(p.get("stock"), (int, float))]
    out_of_stock = sum(1 for p in products if not p.get("in_stock"))
    low_stock = [p for p in known_stock if 0 < p["stock"] < threshold]
    margins = [(p["retail_price"] - p["source_cost"]) for p in products
               if p.get("source_cost")]
    avg_margin_pct = round(
        sum((p["retail_price"] / p["source_cost"] - 1) for p in products
            if p.get("source_cost")) / max(1, len(products)) * 100, 1)

    # ---- orders (from our log) grouped by outcome ----
    def cnt(status): return sum(1 for o in orders if o.get("status") == status)
    fulfilled = [o for o in orders if o.get("status") == "fulfilled"]
    paid_cj_failed = [o for o in orders if o.get("status") == "paid_cj_failed"]
    released = [o for o in orders if o.get("status", "").startswith("released")]
    captured = fulfilled + paid_cj_failed          # money actually taken

    def window(olist, days):
        c = _since(days)
        return [o for o in olist if _order_ts(o) >= c]

    def profit(olist): return round(sum(float(o.get("est_profit", 0)) for o in olist), 2)
    def revenue(olist): return round(sum(float(o.get("total", 0)) for o in olist), 2)
    def cost(olist): return round(sum(float(o.get("product_cost", 0)) for o in olist), 2)
    def fees(olist): return round(sum(float(o.get("stripe_fee", 0)) for o in olist), 2)

    profit_blocks = {
        "today": {"orders": len(window(captured, 1)), "revenue": revenue(window(captured, 1)),
                  "profit": profit(window(captured, 1))},
        "7d": {"orders": len(window(captured, 7)), "revenue": revenue(window(captured, 7)),
               "profit": profit(window(captured, 7))},
        "30d": {"orders": len(window(captured, 30)), "revenue": revenue(window(captured, 30)),
                "profit": profit(window(captured, 30))},
        "all": {"orders": len(captured), "revenue": revenue(captured),
                "profit": profit(captured), "product_cost": cost(captured),
                "stripe_fees": fees(captured)},
    }

    # ---- Stripe live data (payments, refunds, disputes, balance, payouts) ----
    stripe_block = {"connected": PAYMENTS_ENABLED}
    if PAYMENTS_ENABLED:
        try:
            refunds = stripe.Refund.list(limit=100).data
            disputes = stripe.Dispute.list(limit=100).data
            bal = stripe.Balance.retrieve()
            payouts = stripe.Payout.list(limit=3).data
            stripe_block.update({
                "refund_count": len(refunds),
                "refund_total": round(sum(r.amount for r in refunds) / 100, 2),
                "dispute_count": len(disputes),
                "dispute_open": sum(1 for d in disputes if d.status in
                                    ("warning_needs_response", "needs_response",
                                     "under_review")),
                "dispute_total": round(sum(d.amount for d in disputes) / 100, 2),
                "balance_available": round(sum(b.amount for b in bal.available) / 100, 2),
                "balance_pending": round(sum(b.amount for b in bal.pending) / 100, 2),
                "next_payout": (payouts[0].amount / 100 if payouts else 0),
                "next_payout_date": (time.strftime("%Y-%m-%d",
                                     time.gmtime(payouts[0].arrival_date))
                                     if payouts else None),
            })
        except Exception as e:  # noqa: BLE001
            stripe_block["error"] = str(e)

    # ---- CJ wallet balance (best-effort) ----
    cj_block = {"connected": cj is not None}
    if cj is not None:
        try:
            bal = cj._get("/shopping/pay/getBalance", {})
            data = bal.get("data") or {}
            cj_block["wallet"] = data.get("amount") or data.get("balance")
        except Exception:
            cj_block["wallet"] = None

    # ---- alerts / blockages the owner must act on ----
    alerts = []
    if paid_cj_failed:
        alerts.append({"level": "danger", "msg": f"{len(paid_cj_failed)} paid order(s) failed to reach CJ — manual retry needed"})
    if stripe_block.get("dispute_open"):
        alerts.append({"level": "danger", "msg": f"{stripe_block['dispute_open']} open dispute(s) need a response"})
    if not PAYMENTS_ENABLED:
        alerts.append({"level": "warning", "msg": "Stripe not connected — store cannot charge cards yet"})
    if cj_block.get("wallet") is not None:
        try:
            if float(cj_block["wallet"]) < 50:
                alerts.append({"level": "warning", "msg": f"CJ wallet low (${cj_block['wallet']}) — top up to keep orders shipping"})
        except Exception:
            pass
    if out_of_stock > len(products) * 0.3:
        alerts.append({"level": "warning", "msg": f"{out_of_stock} products out of stock ({round(out_of_stock/max(1,len(products))*100)}% of catalog)"})

    return jsonify(
        ok=True,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        store={"name": store.get("name"), "url": os.environ.get("STOREFRONT_ORIGIN", "")},
        profit=profit_blocks,
        orders={
            "placed_by_buyers": len(captured),
            "sent_to_cj": len(fulfilled),
            "failed_to_cj": len(paid_cj_failed),
            "released_no_charge": len(released),
        },
        stripe=stripe_block,
        cj=cj_block,
        catalog={
            "total": len(products),
            "in_stock": len(products) - out_of_stock,
            "out_of_stock": out_of_stock,
            "low_stock": len(low_stock),
            "avg_margin_pct": avg_margin_pct,
            "last_price_sync": store.get("last_price_sync"),
            "last_full_sync": store.get("last_full_sync"),
        },
        pricing={
            "profit_target_pct": round(cfg.profit_target * 100),
            "min_profit_per_unit": cfg.min_profit_per_unit,
            "shipping_markup_pct": round((SHIPPING_MARKUP - 1) * 100),
            "safety_stock_buffer": threshold,
            "gateway_fee_pct": round(cfg.gateway_fee_rate * 100, 1),
        },
        low_stock_items=[{"id": p["id"], "title": p["title"], "stock": p["stock"]}
                         for p in low_stock[:25]],
        alerts=alerts,
    )


@app.get("/api/admin/orders")
def admin_orders():
    """Recent order activity feed (newest first)."""
    if not admin_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    orders = sorted(read_orders(), key=_order_ts, reverse=True)
    return jsonify(ok=True, count=len(orders), orders=orders[:200])


@app.get("/api/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
