"""
bootstrap_catalog.py — replace the demo catalog with REAL CJ products.

Pages through CJ's product list (US-warehouse first), normalizes each row
into the storefront shape with the risk-adjusted retail price, and writes:

  ../products.json      the live catalog (real titles, real photos, real costs)
  watchlist.json        one entry per pid so the daily deep sync can enrich
                        them (variant ids, full image sets, live stock)

Honesty rules baked in:
  - No fabricated ratings/review counts (fields omitted; storefront hides them)
  - stock unknown until verified -> no fake "Only N left" badges
  - price = max of CJ's price range so no variant sells below cost

Designed to run in GitHub Actions (CJ_API_KEY secret in env):
  python bootstrap_catalog.py --count 520 --country US
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

from cj_client import CJClient, CJError
from pricing import PricingConfig, retail_price
from sync_engine import atomic_write, now_iso

HERE = Path(__file__).resolve().parent
PRODUCTS_JSON = HERE.parent / "products.json"
WATCHLIST = HERE / "watchlist.json"

THROTTLE_S = 1.2          # CJ rate-limits ~1 req/s on product endpoints


def parse_cost(row: dict) -> float:
    """sellPrice may be '3.99' or a range '3.99 -- 12.50'; price the high end."""
    raw = str(row.get("sellPrice") or row.get("price") or "0")
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", raw)]
    return max(nums) if nums else 0.0


def parse_image(row: dict) -> str:
    img = row.get("productImage") or row.get("bigImage") or ""
    if isinstance(img, list):
        return img[0] if img else ""
    img = str(img)
    if img.startswith("["):          # sometimes a JSON-encoded list string
        try:
            arr = json.loads(img)
            return arr[0] if arr else ""
        except Exception:
            pass
    return img


def normalize(row: dict, cfg: PricingConfig, idx: int) -> dict | None:
    pid = row.get("pid")
    cost = parse_cost(row)
    image = parse_image(row)
    title = (row.get("productNameEn") or row.get("productName") or "").strip()
    if not pid or not title or not image or cost <= 0:
        return None
    listed = int(row.get("listedNum") or 0)
    return {
        "id": f"CJ-{idx:04d}",
        "cj_pid": pid,
        "cj_vid": "",                        # filled by the daily deep sync
        "title": title,
        "image": image,
        "images": [image],
        "description": "",                   # deep sync fills this
        "category": (row.get("categoryName") or "General").strip(),
        # NOTE: no rating / review_count — we don't invent social proof.
        "source_cost": round(cost, 2),
        "retail_price": retail_price(cost, cfg),
        "stock": None,                        # unknown until verified
        "in_stock": True,                     # checkout re-verifies before charge
        "trending_score": min(0.99, listed / 10000.0),
        "source_verified_at": None,           # set by first real sync
    }


def fetch_pages(cj: CJClient, count: int, country: str | None) -> list[dict]:
    """Page listV2 until we have `count` unique products. US warehouse first,
    then fill from the global catalog if needed."""
    got: dict[str, dict] = {}
    passes = [country, None] if country else [None]
    for cc in passes:
        page = 1
        while len(got) < count and page <= 40:
            try:
                data = cj.list_products(page=page, size=100, country_code=cc)
            except CJError as e:
                print(f"[warn] list page {page} (cc={cc}) failed: {e}", file=sys.stderr)
                break
            rows = data["list"]
            if not rows:
                break
            for row in rows:
                if row.get("pid") and row["pid"] not in got:
                    got[row["pid"]] = row
            print(f"[fetch] cc={cc or 'ALL'} page {page}: total unique {len(got)}")
            page += 1
            time.sleep(THROTTLE_S)
        if len(got) >= count:
            break
    return list(got.values())[: count + 50]   # small buffer before filtering


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=520)
    ap.add_argument("--country", default="US",
                    help="prefer this warehouse country code ('' = global)")
    args = ap.parse_args()

    doc = json.loads(PRODUCTS_JSON.read_text())
    cfg = PricingConfig.from_store(doc["store"])

    cj = CJClient()
    rows = fetch_pages(cj, args.count, args.country or None)

    products = []
    for row in rows:
        p = normalize(row, cfg, len(products) + 1)
        if p:
            products.append(p)
        if len(products) >= args.count:
            break

    if len(products) < 100:
        print(f"[abort] only {len(products)} usable products — keeping existing "
              "catalog rather than degrading it.", file=sys.stderr)
        sys.exit(1)

    doc["products"] = products
    doc["store"]["last_full_sync"] = now_iso()
    doc["store"]["last_price_sync"] = now_iso()
    atomic_write(PRODUCTS_JSON, doc)

    wl = {
        "_comment": "Auto-generated by bootstrap_catalog.py — one entry per "
                    "listed product. The daily deep sync enriches these "
                    "(variant ids, image sets, verified stock).",
        "items": [{
            "sku": p["id"],
            "pid": p["cj_pid"],
            "vid": "",
            "title": p["title"],
            "category": p["category"],
            "trending_score": p["trending_score"],
        } for p in products],
    }
    WATCHLIST.write_text(json.dumps(wl, indent=1))

    cats = {}
    for p in products:
        cats[p["category"]] = cats.get(p["category"], 0) + 1
    print(f"[done] wrote {len(products)} real products across {len(cats)} CJ categories")


if __name__ == "__main__":
    main()
