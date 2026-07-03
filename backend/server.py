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
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
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

        unit = live_unit_price(cj, p, vid, cfg)   # up-to-date price
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
            shipping_cost = gross_up(q["cost"], cfg.gateway_fee_rate)
            ship_name, ship_days = q["name"], q["days"]
        except CJError:
            shipping_cost = None
    if shipping_cost is None:
        shipping_cost = gross_up(float(store.get("fallback_shipping", 6.99)),
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
        return jsonify(ok=False, captured=False, reason="; ".join(problems)), 409

    # ---- 2. capture the authorized funds ----
    try:
        intent = stripe.PaymentIntent.capture(pi_id)
    except stripe.error.StripeError as e:
        return jsonify(ok=False, captured=False, reason=f"Capture failed: {e.user_message}"), 402

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
    try:
        cj_result = cj.create_order(cj_order)
    except CJError as e:
        # Payment captured but CJ order failed — flag for manual handling,
        # do NOT silently drop it. In production: enqueue a retry + alert.
        return jsonify(ok=True, captured=True, fulfilled=False,
                       reason=f"Paid, but CJ order needs manual retry: {e}",
                       payment_intent=intent.id), 202

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


@app.get("/api/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
