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
# Public storefront can live on any domain (github.io, vellow.pages.dev,
# vellow.com …), so allow all origins. Sensitive endpoints are token-gated
# (admin) or require the full payment flow; nothing secret is exposed by origin.
CORS(app, origins="*")

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

# Auto-pay CJ orders from the wallet balance right after they're created, so
# fulfillment is fully hands-off. This SPENDS REAL MONEY per order, so it is
# OFF by default: orders are created in CJ but left unpaid until you either pay
# them in the CJ dashboard or turn this on. Enable only after your CJ wallet is
# funded — set env CJ_AUTO_PAY=1 (or true/yes). Any other value keeps it off.
CJ_AUTO_PAY = os.environ.get("CJ_AUTO_PAY", "").strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Coupons — a code grants a percentage off the product subtotal (not tax or
# shipping). Applied server-side in the quote + hold so a customer can't fake a
# discount. Configure codes via env COUPONS_JSON (a JSON object) to add/change
# them in Render without a redeploy; otherwise these defaults apply. A % off a
# GENUINE listed price is a legitimate promotion — keep any deep "sale" code
# time-limited (set "expires") and never advertise a permanent sitewide sale.
# ---------------------------------------------------------------------------
def _load_coupons() -> dict:
    raw = os.environ.get("COUPONS_JSON", "").strip()
    if raw:
        try:
            return {str(k).upper(): v for k, v in json.loads(raw).items()}
        except Exception as e:  # noqa: BLE001
            print(f"[coupons] bad COUPONS_JSON, using defaults: {e}")
    return {
        "WELCOME15": {"pct": 0.15},   # email-signup, first-order discount
        "SALE50":    {"pct": 0.50},   # for GENUINE, time-limited sales only
    }
COUPONS = _load_coupons()
WELCOME_CODE = os.environ.get("WELCOME_CODE", "WELCOME15")
SIGNUPS_LOG = Path(os.environ.get("SIGNUPS_LOG", str(HERE / "signups.jsonl")))

def resolve_coupon(code):
    """Return {'code','pct'} for a valid coupon, else None. Honors an optional
    'expires' (YYYY-MM-DD) and hard-caps any single discount at 90% for safety."""
    if not code:
        return None
    c = COUPONS.get(str(code).strip().upper())
    if not isinstance(c, dict):
        return None
    exp = c.get("expires")
    if exp and time.strftime("%Y-%m-%d", time.gmtime()) > str(exp):
        return None
    try:
        pct = max(0.0, min(float(c.get("pct", 0)), 0.90))
    except (TypeError, ValueError):
        return None
    if pct <= 0:
        return None
    return {"code": str(code).strip().upper(), "pct": pct}

# ---------------------------------------------------------------------------
# Transactional email (Resend). Set RESEND_API_KEY in the Render env to enable.
# EMAIL_FROM should be a verified sender once you have a domain; until then the
# Resend onboarding address works for testing (deliverability is limited).
# If no key is set, email is skipped gracefully (never blocks checkout).
# ---------------------------------------------------------------------------
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Vellow <onboarding@resend.dev>")
STORE_URL = os.environ.get("STOREFRONT_ORIGIN", "https://vellow-five.vercel.app")

# CJ order page the owner dashboard links to when paying an order (report only;
# the owner is notified on the admin page, not by email).
CJ_ORDERS_URL = os.environ.get("CJ_ORDERS_URL",
                               "https://app.cjdropshipping.com/myCJ.html#/order")


def send_email(to: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY or not to:
        print(f"[email] skipped (no key or recipient): {subject}")
        return False
    try:
        r = requests.post("https://api.resend.com/emails",
                          headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                                   "Content-Type": "application/json"},
                          json={"from": EMAIL_FROM, "to": [to],
                                "subject": subject, "html": html}, timeout=15)
        if r.status_code >= 300:
            print(f"[email] failed {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001 — email must never break the order
        print(f"[email] error: {e}")
        return False


def _email_shell(inner: str) -> str:
    return (f'<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;'
            f'color:#1A1A1A">'
            f'<div style="background:#0C2340;color:#fff;padding:16px 20px;font-size:22px;'
            f'font-weight:800">VEL<span style="color:#FF4500">LOW</span></div>'
            f'<div style="padding:20px">{inner}'
            f'<p style="color:#888;font-size:12px;margin-top:24px">Questions? Just reply to '
            f'this email. Vellow · <a href="{STORE_URL}">{STORE_URL}</a></p></div></div>')


def send_order_confirmation(email: str, order_number: str, items: list,
                            total: float, dest: str):
    rows = "".join(
        f'<tr><td style="padding:4px 0">{i.get("title","Item")}</td>'
        f'<td style="padding:4px 0;text-align:right">× {i.get("qty",1)}</td></tr>'
        for i in items)
    inner = (
        f'<h2 style="margin:0 0 8px">Thanks for your order!</h2>'
        f'<p>Your payment is confirmed and your order is on its way to our supplier for '
        f'fulfillment. Order <b>#{order_number[-10:]}</b>.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin:12px 0;font-size:14px">'
        f'{rows}<tr><td style="border-top:1px solid #eee;padding-top:8px"><b>Total paid</b></td>'
        f'<td style="border-top:1px solid #eee;padding-top:8px;text-align:right">'
        f'<b>${total:.2f}</b></td></tr></table>'
        f'<p style="font-size:14px"><b>Shipping to:</b> {dest}</p>'
        f'<div style="background:#f6f8fb;border-radius:8px;padding:12px;font-size:14px;margin:12px 0">'
        f'📦 <b>Tracking to follow.</b> We\'ll email you a tracking link as soon as your order '
        f'ships (usually within a few days).</div>')
    return send_email(email, f"Your Vellow order is confirmed (#{order_number[-10:]})",
                      _email_shell(inner))


def send_tracking_email(email: str, order_number: str, tracking: str,
                        carrier: str = "", url: str = ""):
    link = url or (f"https://t.17track.net/en#nums={tracking}" if tracking else "")
    inner = (
        f'<h2 style="margin:0 0 8px">Your order has shipped 🚚</h2>'
        f'<p>Good news — order <b>#{order_number[-10:]}</b> is on its way.</p>'
        f'<p style="font-size:14px"><b>Carrier:</b> {carrier or "—"}<br>'
        f'<b>Tracking number:</b> {tracking}</p>'
        + (f'<p><a href="{link}" style="display:inline-block;background:#FF4500;color:#fff;'
           f'text-decoration:none;padding:10px 18px;border-radius:6px;font-weight:bold">'
           f'Track your package</a></p>' if link else ''))
    return send_email(email, f"Your Vellow order has shipped (#{order_number[-10:]})",
                      _email_shell(inner))

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


def build_quote(catalog: dict, items: list, shipping: dict, coupon_code=None) -> dict:
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
    # Coupon: percentage off the product subtotal only (not tax/shipping).
    # Tax is then computed on what the customer actually pays after the discount.
    coupon = resolve_coupon(coupon_code)
    discount = round(subtotal * coupon["pct"], 2) if coupon else 0.0
    disc_subtotal = round(subtotal - discount, 2)
    tax = round(disc_subtotal * rate, 2)
    total = round(disc_subtotal + tax + shipping_cost, 2)
    return {
        "lines": lines, "problems": problems,
        "subtotal": round(subtotal, 2),
        "discount": discount,
        "coupon": coupon["code"] if coupon else None,
        "coupon_pct": coupon["pct"] if coupon else 0,
        "coupon_invalid": bool(coupon_code) and coupon is None,
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
    q = build_quote(load_catalog(), body.get("items", []), body.get("shipping", {}),
                    body.get("coupon"))
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

    q = build_quote(catalog, body.get("items", []), body.get("shipping", {}),
                    body.get("coupon"))
    if q["problems"]:
        return jsonify(ok=False, reason="; ".join(q["problems"])), 409

    amount = round(q["total"] * 100)  # Stripe uses integer cents (discount included)
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
                   subtotal=q["subtotal"], discount=q["discount"], coupon=q["coupon"],
                   tax=q["tax"], shipping=q["shipping"], total=q["total"])


# ---------------------------------------------------------------------------
@app.post("/api/signup")
def signup():
    """Email capture in exchange for the welcome discount code. Stores the email
    with an opt-in timestamp and returns the code. Legitimate lead capture:
    a real discount for a real email, with consent to be contacted."""
    body = request.get_json(force=True)
    email = (body.get("email") or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify(ok=False, reason="Please enter a valid email address."), 400
    try:
        with open(SIGNUPS_LOG, "a") as f:
            f.write(json.dumps({"email": email, "opt_in": True,
                                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")
    except Exception as e:  # noqa: BLE001 — capture must never 500
        print(f"[signup] could not record {email}: {e}")
    return jsonify(ok=True, code=WELCOME_CODE)


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
    # CJ requires a shipping method (logisticName). Use the cheapest live option.
    logistic_name = "CJPacket Ordinary"
    try:
        sq = cj.get_shipping_quote_multi(
            [{"vid": l["vid"], "quantity": l["qty"]} for l in verified_lines],
            ship.get("country", "US"), ship.get("zip", ""), ship.get("state", ""))
        logistic_name = sq.get("name") or logistic_name
    except Exception:
        pass
    _country_names = {"US": "United States", "CA": "Canada", "GB": "United Kingdom",
                      "AU": "Australia", "DE": "Germany", "FR": "France"}
    _cc = ship.get("country", "US")
    cj_order = {
        "orderNumber": intent.id,                       # your idempotency key
        "fromCountryCode": "CN",                         # origin warehouse (CJ routes)
        "logisticName": logistic_name,                   # shipping method (required)
        "shippingCountryCode": _cc,
        "shippingCountry": _country_names.get(_cc, _cc), # full country name (required)
        "email": ship.get("email", ""),
        "houseNumber": "1",
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

    # Order confirmation email (fires on every captured order, regardless of the
    # CJ forward outcome). Tracking is emailed separately once CJ provides it.
    send_order_confirmation(ship.get("email", ""), intent.id, base["items"],
                            order_total, base["dest"])

    try:
        cj_result = cj.create_order(cj_order)
    except Exception as e:  # noqa: BLE001 — any CJ failure must not 500 after capture
        # Payment captured but CJ order failed — flag for manual handling,
        # do NOT silently drop it and do NOT crash. In production: enqueue a
        # retry + alert. The order shows in the admin dashboard as "failed to CJ".
        print(f"[cj-order-failed] {e}")  # surface CJ's reason in the logs
        # Shows on the owner dashboard as "CJ failed" — needs manual placement.
        log_order({**base, "status": "paid_cj_failed", "reason": str(e),
                   "supplier_cost": prod_cost})
        return jsonify(ok=True, captured=True, fulfilled=False,
                       reason=f"Paid, but CJ order needs manual retry: {e}",
                       payment_intent=intent.id), 202

    cj_order_id = ((cj_result or {}).get("orderId")
                   or (cj_result or {}).get("orderNum") or "")

    # ---- 4. pay the CJ order from wallet balance (optional, gated) ----
    # createOrderV2 only creates the order; CJ won't ship until it's paid.
    # When CJ_AUTO_PAY is enabled AND the wallet is funded, pay it now so the
    # whole flow is hands-off. Failure here (e.g. unfunded wallet) never 500s a
    # captured order — it's logged as "order_placed" (unpaid) for manual pay.
    pay_status = "order_placed"        # created in CJ, not yet paid
    pay_reason = ""
    if CJ_AUTO_PAY and cj_order_id:
        try:
            cj.pay_order(cj_order_id, order_number=intent.id)
            pay_status = "order_paid"  # paid → CJ will fulfill & ship
        except Exception as e:  # noqa: BLE001 — never crash a captured order
            pay_reason = str(e)
            print(f"[cj-pay-failed] {cj_order_id}: {e}")

    # Orders left unpaid surface on the OWNER's admin dashboard as an "awaiting
    # payment" report (status "order_placed") — the owner is notified there, not
    # by email. Customer emails (confirmation + tracking) are separate.
    log_order({**base, "status": pay_status,        # awaiting tracking from CJ
               "cj_order_id": cj_order_id,
               "supplier_cost": prod_cost,          # shown in the pay report
               **({"pay_reason": pay_reason} if pay_reason else {})})

    return jsonify(ok=True, captured=True, fulfilled=True, paid=(pay_status == "order_paid"),
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
    fulfilled = [o for o in orders if o.get("status") in ("order_placed", "fulfilled")]
    tracking_sent = [o for o in orders if o.get("status") == "tracking_sent"]
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
            "tracking_emailed": len(tracking_sent),
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


@app.post("/api/admin/refresh-tracking")
def admin_refresh_tracking():
    """Automatic tracking dispatch. For every 'order placed' order that has a CJ
    order id and hasn't had tracking emailed yet, ask CJ for tracking; the moment
    it exists, email the customer and mark it sent. Meant to be hit on a schedule.
    """
    if not admin_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    if cj is None:
        return jsonify(ok=False, reason="CJ not configured"), 503

    orders = read_orders()
    already_sent = {o.get("pi") for o in orders if o.get("status") == "tracking_sent"}
    placed = [o for o in orders if o.get("status") == "order_placed"
              and o.get("cj_order_id") and o.get("pi") not in already_sent]

    checked, sent = 0, 0
    for o in placed:
        checked += 1
        try:
            tr = cj.get_tracking(o["cj_order_id"])
        except Exception:  # noqa: BLE001
            tr = None
        if not tr:
            continue
        if send_tracking_email(o.get("email", ""), o.get("pi", ""),
                               tr["tracking"], tr.get("carrier", "")):
            sent += 1
            log_order({"status": "tracking_sent", "pi": o.get("pi", ""),
                       "email": o.get("email", ""), "tracking": tr["tracking"],
                       "carrier": tr.get("carrier", "")})
        time.sleep(0.5)
    return jsonify(ok=True, checked=checked, tracking_emails_sent=sent)


@app.post("/api/admin/send-tracking")
def admin_send_tracking():
    """Email a shipment-tracking link to a customer. Owner triggers this from
    the dashboard once CJ provides a tracking number for an order.
    Body: { email, order_number, tracking, carrier?, url? }"""
    if not admin_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    b = request.get_json(force=True)
    if not b.get("email") or not b.get("tracking"):
        return jsonify(ok=False, reason="email and tracking required"), 400
    sent = send_tracking_email(b["email"], b.get("order_number", ""),
                               b["tracking"], b.get("carrier", ""), b.get("url", ""))
    # record it on the order log for the dashboard feed
    log_order({"status": "tracking_sent", "email": b["email"],
               "pi": b.get("order_number", ""), "tracking": b["tracking"],
               "carrier": b.get("carrier", "")})
    return jsonify(ok=sent, sent=sent)


@app.get("/api/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
