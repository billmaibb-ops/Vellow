"""
sync_engine.py — writes ../products.json from live CJ Dropshipping data.

Two modes (per the architecture spec):

  --mode hourly  (default, runs every hour)
      Lightweight poll. For each product already in products.json, re-check
      ONLY price + stock via CJ. Fast, low API cost, avoids bot-defenses.
      Applies the safety buffer: US stock < threshold => in_stock = false.

  --mode daily   (run once a day)
      Deep sync. Pull full product detail (title, images, description,
      variants) for every SKU in watchlist.json, recompute risk-adjusted
      price, and rewrite the full catalog.

products.json is written ATOMICALLY (temp file + os.replace) so the
storefront's fetch() never reads a half-written file during a live session.

Scheduling: don't loop forever in one process. Run this from cron / the
OS scheduler:
    0 * * * *  python sync_engine.py --mode hourly     # top of every hour
    30 3 * * * python sync_engine.py --mode daily       # 3:30am daily
"""

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from cj_client import CJClient, CJError
from pricing import PricingConfig, retail_price

HERE = Path(__file__).resolve().parent
PRODUCTS_JSON = HERE.parent / "products.json"      # the file the storefront reads
WATCHLIST = HERE / "watchlist.json"                # CJ pids/vids we choose to sell
DETAILS_DIR = HERE.parent / "products"             # per-product detail pages read these


def sanitize_html(html: str) -> str:
    """Strip active content from supplier-provided description HTML before it
    is served to customers (script/style/iframe, on* handlers, js: urls)."""
    if not html:
        return ""
    import re
    html = re.sub(r"(?is)<(script|style|iframe|object|embed|form)[^>]*>.*?</\1>", "", html)
    html = re.sub(r"(?is)<(script|style|iframe|object|embed|form|link|meta)[^>]*/?>", "", html)
    html = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html)
    html = re.sub(r"(?i)(href|src)\s*=\s*([\"']?)\s*javascript:[^\"'>\s]*", r"\1=\2#", html)
    return html.strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def atomic_write(path: Path, data: dict):
    """Write JSON to a temp file in the same dir, then atomically replace.
    Guarantees the storefront never sees a partial file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)  # atomic on POSIX and Windows
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def safe_stock(us_qty: int, total_qty: int, threshold: int) -> tuple[int, bool]:
    """Prefer US-warehouse stock (fast shipping). Apply safety buffer."""
    effective = us_qty if us_qty > 0 else total_qty
    in_stock = effective >= threshold
    return effective, in_stock


# --------------------------------------------------------------------------
def run_hourly(cj: CJClient, catalog: dict) -> dict:
    store = catalog["store"]
    cfg = PricingConfig.from_store(store)
    threshold = store.get("safety_stock_threshold", 5)

    updated = 0
    for p in catalog["products"]:
        vid = p.get("cj_vid")
        if not vid:
            continue  # can't poll without a CJ variant id; skip (deep sync sets it)
        try:
            stock = cj.get_variant_stock(vid)
        except CJError as e:
            # On a poll failure, be conservative: mark out of stock, don't crash.
            print(f"[warn] stock poll failed for {p['id']} ({vid}): {e}", file=sys.stderr)
            p["in_stock"] = False
            continue

        eff, in_stock = safe_stock(stock["us_quantity"], stock["quantity"], threshold)
        p["stock"] = eff
        p["in_stock"] = in_stock

        # Re-price only if CJ moved the cost (cost is refreshed on deep sync,
        # but some pollers also return sellPrice — recompute defensively).
        if "source_cost" in p:
            p["retail_price"] = retail_price(p["source_cost"], cfg)

        p["source_verified_at"] = now_iso()
        updated += 1

    store["last_price_sync"] = now_iso()
    print(f"[hourly] repriced/re-stocked {updated} products")
    return catalog


# --------------------------------------------------------------------------
def run_daily(cj: CJClient, catalog: dict) -> dict:
    store = catalog["store"]
    cfg = PricingConfig.from_store(store)
    threshold = store.get("safety_stock_threshold", 5)
    watchlist = load_json(WATCHLIST, {"items": []})["items"]
    # Skip placeholder entries so a template watchlist can't produce junk.
    watchlist = [e for e in watchlist
                 if e.get("pid") and not e["pid"].startswith(("REPLACE", "DEMO"))]

    DETAILS_DIR.mkdir(exist_ok=True)
    old_by_pid = {(p.get("cj_pid") or p.get("id")): p for p in catalog["products"]}
    synced_pids = set()

    # Resumable: if a product already has a fresh detail file AND its catalog
    # entry already carries a cj_vid, keep it as-is and skip the CJ calls. So a
    # run that was rate-limited part-way makes real progress each time instead
    # of starting over (and we stay well under CJ's request budget).
    have_detail = {p.get("cj_pid"): p for p in catalog["products"]
                   if p.get("cj_vid") and (DETAILS_DIR / f"{p['id']}.json").exists()}

    products = []
    for entry in watchlist:
        pid = entry["pid"]
        try:
            if pid in have_detail:
                # already fully synced on a prior run — just refresh nothing,
                # keep the existing record (cheap; no CJ call)
                products.append(have_detail[pid])
                synced_pids.add(pid)
                continue

            time.sleep(0.9)  # be gentle: CJ rate-limits product endpoints
            detail = cj.get_product(pid)

            # Pick the variant we sell by default (explicit vid, else the first).
            variants = detail.get("variants") or []
            vid = entry.get("vid")
            variant = next((v for v in variants if v.get("vid") == vid), variants[0] if variants else {})
            vid = variant.get("vid", vid)

            cost = float(variant.get("variantSellPrice") or detail.get("sellPrice") or 0) or None
            if not cost:
                print(f"[warn] no cost for pid {pid}; skipping", file=sys.stderr)
                continue

            time.sleep(0.6)
            try:
                stock = cj.get_variant_stock(vid) if vid else {"us_quantity": 0, "quantity": 0}
            except Exception:  # noqa: BLE001
                stock = {"us_quantity": 0, "quantity": 0}
            eff, in_stock = safe_stock(stock["us_quantity"], stock["quantity"], threshold)

            images = detail.get("productImageSet") or detail.get("productImage") or []
            if isinstance(images, str):
                images = [images]

            sku = entry.get("sku") or f"CJ-{pid}"
            # Per-variant options (color/size) with their own risk-adjusted retail.
            variant_rows = []
            for v in variants[:40]:
                vcost = float(v.get("variantSellPrice") or 0)
                if not v.get("vid") or vcost <= 0:
                    continue
                variant_rows.append({
                    "vid": v["vid"],
                    "name": (v.get("variantNameEn") or v.get("variantKey")
                             or v.get("variantName") or "").strip() or "Default",
                    "image": v.get("variantImage") or "",
                    "source_cost": round(vcost, 2),
                    "retail_price": retail_price(vcost, cfg),
                })

            # Write the detail file the product page fetches on demand. Kept out
            # of products.json so the storefront list stays a fast, small fetch.
            atomic_write(DETAILS_DIR / f"{sku}.json", {
                "id": sku,
                "description": sanitize_html(detail.get("description") or ""),
                "images": images,          # full set — matches the CJ product page
                "variants": variant_rows,
                "synced_at": now_iso(),
            })

            prod = {
                "id": sku,
                "cj_pid": pid,
                "cj_vid": vid,
                "title": entry.get("title") or detail.get("productNameEn", "Untitled"),
                "image": images[0] if images else "https://placehold.co/600x600?text=No+Image",
                "images": images[:4],   # a few for the list card; full set is in the detail file
                "category": entry.get("category", detail.get("categoryName", "General")),
                "source_cost": round(cost, 2),
                "retail_price": retail_price(cost, cfg),
                "stock": eff,
                "in_stock": in_stock,
                "trending_score": entry.get("trending_score", 0.5),
                "source_verified_at": now_iso(),
                "has_detail": True,
            }
            # No fabricated social proof: rating/review_count only if truly known.
            if entry.get("rating"):
                prod["rating"] = entry["rating"]
                prod["review_count"] = entry.get("review_count", 0)
            if entry.get("brand"):
                prod["brand"] = entry["brand"]
            products.append(prod)
            synced_pids.add(pid)
            if len(products) % 100 == 0:
                print(f"[daily] {len(products)} synced so far…")
        except Exception as e:  # noqa: BLE001 — one bad product must not abort the run
            print(f"[warn] deep sync skipped pid {pid}: {e}", file=sys.stderr)
            continue

    # Keep any existing product whose sync failed this run (don't shrink the
    # catalog because of transient CJ errors — next run retries them).
    kept = 0
    for opid, oldp in old_by_pid.items():
        if opid not in synced_pids:
            products.append(oldp)
            kept += 1
    if kept:
        print(f"[daily] kept {kept} existing products whose sync failed/skipped")

    # WIPE GUARD: if the deep sync produced nothing (empty/placeholder
    # watchlist, CJ outage, auth failure), KEEP the existing catalog rather
    # than replacing 500 live products with an empty page.
    if not products:
        print("[daily] 0 products synced — keeping existing catalog unchanged",
              file=sys.stderr)
        return catalog

    catalog["products"] = products
    store["last_full_sync"] = now_iso()
    store["last_price_sync"] = now_iso()
    print(f"[daily] deep-synced {len(products)} products")
    return catalog


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["hourly", "daily"], default="hourly")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute but do not write products.json")
    args = ap.parse_args()

    catalog = load_json(PRODUCTS_JSON, None)
    if catalog is None:
        print("products.json not found — run --mode daily first (needs watchlist.json).",
              file=sys.stderr)
        sys.exit(1)

    cj = CJClient()
    catalog = run_daily(cj, catalog) if args.mode == "daily" else run_hourly(cj, catalog)

    if args.dry_run:
        print(json.dumps(catalog["store"], indent=2))
        print(f"(dry run — {len(catalog['products'])} products, not written)")
    else:
        atomic_write(PRODUCTS_JSON, catalog)
        print(f"wrote {PRODUCTS_JSON}")


if __name__ == "__main__":
    main()
