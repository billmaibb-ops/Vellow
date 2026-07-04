"""
check_cj_order.py — diagnostic: attempt a CJ createOrderV2 and print the exact
response/error, so we know why order forwarding fails (e.g. insufficient wallet
balance, missing required field, bad vid). Does NOT involve Stripe.

Usage (via GitHub Actions, which can reach CJ):
  python check_cj_order.py --pid <PID> --zip 10118 --state NY
"""
import argparse
from cj_client import CJClient, CJError


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", default="1737398203613454336")
    ap.add_argument("--zip", default="10118")
    ap.add_argument("--state", default="NY")
    ap.add_argument("--city", default="New York")
    ap.add_argument("--qty", type=int, default=1)
    args = ap.parse_args()

    cj = CJClient()
    detail = cj.get_product(args.pid)
    variants = detail.get("variants") or []
    if not variants:
        print("No variants for product; cannot build order."); return
    vid = variants[0].get("vid")
    print(f"product: {detail.get('productNameEn','?')[:50]} | vid: {vid}")

    order = {
        "orderNumber": f"TEST-{args.pid[-6:]}-{args.zip}",
        "fromCountryCode": "CN",
        "shippingCountryCode": "US",
        "shippingProvince": args.state,
        "shippingCity": args.city,
        "shippingAddress": "350 Fifth Avenue",
        "shippingCustomerName": "Test Buyer",
        "shippingZip": args.zip,
        "shippingPhone": "2125551234",
        "remark": "diagnostic test order",
        "products": [{"vid": vid, "quantity": args.qty}],
    }
    print("\nAttempting createOrderV2 …")
    try:
        res = cj.create_order(order)
        print("SUCCESS — CJ accepted the order:")
        print(res)
    except CJError as e:
        print("CJ REJECTED THE ORDER — exact reason:")
        print(f"  {e}")
    except Exception as e:  # noqa: BLE001
        print(f"Unexpected error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
