"""
check_shipping.py — one-off proof that live CJ shipping works.

Given a product (pid) and a destination address, resolves the product's
variant id from CJ, then asks CJ's freight calculator for the real shipping
options to that address and prints the cheapest. This is the exact call the
checkout backend makes on every order — run here via GitHub Actions (which
can reach CJ) to demonstrate it end to end.

Usage:
  python check_shipping.py --pid <PID> --zip 90210 --state CA --qty 1
"""

import argparse
import json
from cj_client import CJClient, CJError


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", required=True)
    ap.add_argument("--zip", required=True)
    ap.add_argument("--country", default="US")
    ap.add_argument("--state", default="")
    ap.add_argument("--qty", type=int, default=1)
    args = ap.parse_args()

    cj = CJClient()

    print(f"Looking up product {args.pid} …")
    detail = cj.get_product(args.pid)
    variants = detail.get("variants") or []
    if not variants:
        print("No variants returned for this product — cannot quote shipping.")
        return
    v = variants[0]
    vid = v.get("vid")
    print(f"  title : {detail.get('productNameEn','?')[:60]}")
    print(f"  vid   : {vid}")
    print(f"  cost  : {v.get('variantSellPrice')}")

    print(f"\nAsking CJ for shipping to {args.country}/{args.zip} "
          f"(state {args.state or 'n/a'}), qty {args.qty} …")
    try:
        q = cj.get_shipping_quote_multi(
            [{"vid": vid, "quantity": args.qty}], args.country, args.zip, args.state)
    except CJError as e:
        print(f"  CJ returned no shipping options: {e}")
        return

    print("\n=== CHEAPEST LIVE SHIPPING OPTION ===")
    print(f"  carrier      : {q['name']}")
    print(f"  cost         : ${q['cost']:.2f}")
    print(f"  est. days    : {q['days']}")
    print(f"  options seen : {q['options']}")
    print("\n(This is CJ's real cost to you; the storefront grosses it up ~3% "
          "to cover the card fee, then shows it as the shipping line.)")


if __name__ == "__main__":
    main()
