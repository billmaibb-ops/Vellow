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
    raw = str(row.get("sellPrice") or row.get("price")
              or row.get("nowPrice") or row.get("originalPrice") or "0")
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", raw)]
    return max(nums) if nums else 0.0


def parse_image(row: dict) -> str:
    img = (row.get("productImage") or row.get("bigImage")
           or row.get("image") or row.get("imgUrl") or "")
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
    pid = row.get("pid") or row.get("id") or row.get("productId")
    cost = parse_cost(row)
    image = parse_image(row)
    title = (row.get("productNameEn") or row.get("productName")
             or row.get("nameEn") or row.get("name") or "").strip()
    if not pid or not title or not image or cost <= 0:
        return None
    listed = int(row.get("listedNum") or 0)
    # CJ now exposes verified inventory in the list payload — use it so
    # stock badges are honest instead of invented.
    try:
        inv = int(float(row.get("totalVerifiedInventory") or 0)) \
              or int(float(row.get("warehouseInventoryNum") or 0))
    except (TypeError, ValueError):
        inv = 0
    category = (row.get("oneCategoryName") or row.get("twoCategoryName")
                or row.get("threeCategoryName") or row.get("categoryName")
                or "General").strip()
    return {
        "id": f"CJ-{idx:04d}",
        "cj_pid": pid,
        "cj_vid": "",                        # filled by the daily deep sync
        "title": title,
        "image": image,
        "images": [image],
        "description": "",                   # deep sync fills this
        "category": category,
        # NOTE: no rating / review_count — we don't invent social proof.
        "source_cost": round(cost, 2),
        "retail_price": retail_price(cost, cfg),
        "stock": inv if inv > 0 else None,    # None = unknown, no fake badge
        "in_stock": True,                     # checkout re-verifies before charge
        "trending_score": min(0.99, listed / 10000.0),
        "source_verified_at": None,           # set by first real sync
    }


def probe_endpoints(cj: CJClient) -> str:
    """CJ has moved list endpoints between versions. Find one that returns rows
    and show raw responses so failures are debuggable from the Actions log."""
    for path in ("/product/listV2", "/product/list", "/product/myProduct/list"):
        try:
            raw = cj._get(path, {"pageNum": 1, "pageSize": 5})
            data = raw.get("data") or {}
            rows = flatten_rows(data.get("list") or data.get("content") or [])
            print(f"[probe] {path}: code={raw.get('code')} "
                  f"result={raw.get('result')} products={len(rows)} "
                  f"message={raw.get('message')!r}")
            if not rows:
                print(f"[probe] {path} raw: {str(raw)[:400]}")
            else:
                print(f"[probe] {path} first product keys: {sorted(rows[0].keys())}")
                print(f"[probe] first product sample: {str(rows[0])[:400]}")
                return path
        except Exception as e:  # noqa: BLE001
            print(f"[probe] {path} failed: {e}", file=sys.stderr)
    return ""


def flatten_rows(rows: list) -> list[dict]:
    """CJ's listV2 now returns wrapper rows like
    {keyWord, productList, relatedCategoryList} — the real products live in
    productList. Older shapes return product rows directly. Handle both."""
    out: list[dict] = []
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("productList"), list):
            out.extend(x for x in r["productList"] if isinstance(x, dict))
        elif isinstance(r, dict):
            out.append(r)
    return out


def list_page(cj: CJClient, path: str, page: int, size: int,
              country: str | None, keyword: str | None = None) -> list[dict]:
    params: dict = {"pageNum": page, "pageSize": size}
    if country:
        params["countryCode"] = country
    if keyword:
        params["keyWord"] = keyword
    raw = cj._get(path, params)
    data = raw.get("data") or {}
    return flatten_rows(data.get("list") or data.get("content") or [])


# CJ's list endpoint ignores pagination for blank queries (returns the same
# ~10 recommendations per call), so we fan out across many search keywords
# instead — each keyword surfaces a different slice of the catalog.
KEYWORDS = [
    "earbuds", "bluetooth speaker", "power bank", "phone case", "phone holder",
    "usb hub", "webcam", "keyboard", "mouse", "charging cable", "smart watch",
    "led strip", "night light", "desk lamp", "humidifier", "diffuser",
    "shower head", "wall shelf", "storage box", "curtain", "rug", "blanket",
    "pillow", "picture frame", "clock", "candle", "vase", "mirror",
    "milk frother", "vegetable chopper", "knife set", "spice rack",
    "kitchen scale", "cutting board", "water bottle", "coffee", "lunch box",
    "food container", "apron", "measuring cup",
    "laptop stand", "desk organizer", "monitor stand", "notebook", "pen",
    "whiteboard", "mouse pad", "desk mat",
    "makeup brush", "facial roller", "eyelash", "hair brush", "hair clip",
    "nail", "makeup mirror", "makeup bag", "skincare",
    "resistance band", "yoga mat", "jump rope", "massage gun", "dumbbell",
    "posture corrector", "gym gloves", "sports bottle",
    "dog toy", "cat toy", "pet brush", "dog harness", "pet bed", "litter mat",
    "pet feeder", "dog leash",
    "camping lantern", "flashlight", "hammock", "cooler bag", "picnic",
    "hiking", "fishing", "umbrella",
    "car phone mount", "car organizer", "car vacuum", "car charger",
    "car cover", "seat cushion",
    "building blocks", "puzzle", "fidget", "rc car", "plush toy",
    "drawing", "board game", "kids craft",
    "wallet", "sunglasses", "belt", "beanie", "scarf", "backpack",
    "crossbody bag", "keychain", "watch band", "jewelry",
    "baby bib", "baby bottle", "teething", "stroller organizer",
    "baby monitor", "diaper bag", "baby blanket",
    "dress", "t shirt", "leggings", "hoodie", "socks", "swimsuit",
]


def fetch_pages(cj: CJClient, count: int, country: str | None) -> list[dict]:
    """Fan out across many keyword queries (CJ ignores pagination on blank
    queries). US warehouse pass first, then a global pass to fill up."""
    path = probe_endpoints(cj)
    if not path:
        print("[abort] no working product-list endpoint found", file=sys.stderr)
        return []
    got: dict[str, dict] = {}
    passes = [country, None] if country else [None]
    for cc in passes:
        for kw in KEYWORDS:
            page, added = 1, 0
            while page <= 5:
                try:
                    rows = list_page(cj, path, page, 100, cc, kw)
                except Exception as e:  # noqa: BLE001
                    print(f"[warn] kw={kw!r} page {page} (cc={cc}) failed: {e}",
                          file=sys.stderr)
                    break
                if not rows:
                    break
                before = len(got)
                for row in rows:
                    key = row.get("pid") or row.get("id") or row.get("productId")
                    if key and key not in got:
                        got[key] = row
                added += len(got) - before
                if len(got) == before:      # this keyword is exhausted
                    break
                page += 1
                time.sleep(THROTTLE_S)
            print(f"[fetch] cc={cc or 'ALL'} kw={kw!r}: +{added} (total {len(got)})")
            if len(got) >= count:
                return list(got.values())[: count + 100]
            time.sleep(THROTTLE_S)
    return list(got.values())[: count + 100]


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
