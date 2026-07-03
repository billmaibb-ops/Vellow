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
from datetime import datetime, timezone
from pathlib import Path

from cj_client import CJClient, CJError
from pricing import PricingConfig, retail_price

HERE = Path(__file__).resolve().parent
PRODUCTS_JSON = HERE.parent / "products.json"      # the file the storefront reads
WATCHLIST = HERE / "watchlist.json"                # CJ pids/vids we choose to sell


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

    products = []
    for entry in watchlist:
        pid = entry["pid"]
        try:
            detail = cj.get_product(pid)
        except CJError as e:
            print(f"[warn] deep sync failed for pid {pid}: {e}", file=sys.stderr)
            continue

        # Pick the variant we sell (explicit vid, else the first/cheapest).
        variants = detail.get("variants") or []
        vid = entry.get("vid")
        variant = next((v for v in variants if v.get("vid") == vid), variants[0] if variants else {})
        vid = variant.get("vid", vid)

        cost = float(variant.get("variantSellPrice") or detail.get("sellPrice") or 0) or None
        if not cost:
            print(f"[warn] no cost for pid {pid}; skipping", file=sys.stderr)
            continue

        stock = cj.get_variant_stock(vid) if vid else {"us_quantity": 0, "quantity": 0}
        eff, in_stock = safe_stock(stock["us_quantity"], stock["quantity"], threshold)

        images = detail.get("productImageSet") or detail.get("productImage") or []
        if isinstance(images, str):
            images = [images]

        products.append({
            "id": entry.get("sku") or f"CJ-{pid}",
            "cj_pid": pid,
            "cj_vid": vid,
            "title": entry.get("title") or detail.get("productNameEn", "Untitled"),
            "brand": entry.get("brand", "CJ Supplier"),
            "image": images[0] if images else "https://placehold.co/600x600?text=No+Image",
            "images": images[:6],
            "description": detail.get("description") or entry.get("description", ""),
            "category": entry.get("category", detail.get("categoryName", "General")),
            "rating": entry.get("rating", 4.5),
            "review_count": entry.get("review_count", 0),
            "source_cost": round(cost, 2),
            "retail_price": retail_price(cost, cfg),
            "stock": eff,
            "in_stock": in_stock,
            "trending_score": entry.get("trending_score", 0.5),
            "source_verified_at": now_iso(),
        })

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
