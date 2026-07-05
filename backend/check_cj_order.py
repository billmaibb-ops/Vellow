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
    ap.add_argument("--pay", action="store_true",
                    help="also attempt to pay the created order from the CJ "
                         "wallet (spends money only if the wallet is funded)")
    args = ap.parse_args()

    cj = CJClient()

    # Wallet balance first (pure read). Tells us whether a pay attempt would
    # move real money or just return "insufficient balance".
    balance = None
    try:
        bal = cj.get_balance()
        balance = bal["amount"]
        print(f"CJ wallet balance: ${balance:.2f}")
    except Exception as e:  # noqa: BLE001
        print(f"(could not read wallet balance: {e})")

    detail = cj.get_product(args.pid)
    variants = detail.get("variants") or []
    if not variants:
        print("No variants for product; cannot build order."); return
    vid = variants[0].get("vid")
    print(f"product: {detail.get('productNameEn','?')[:50]} | vid: {vid}")

    logistic_name = "CJPacket Ordinary"
    try:
        sq = cj.get_shipping_quote_multi([{"vid": vid, "quantity": args.qty}],
                                         "US", args.zip, args.state)
        logistic_name = sq.get("name") or logistic_name
    except Exception as e:  # noqa: BLE001
        print(f"(shipping quote failed, using default logistic: {e})")
    print(f"logisticName: {logistic_name}")

    order = {
        "orderNumber": f"TEST-{args.pid[-6:]}-{args.zip}",
        "fromCountryCode": "CN",
        "logisticName": logistic_name,
        "shippingCountryCode": "US",
        "shippingCountry": "United States",
        "email": "test@example.com",
        "houseNumber": "1",
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
    order_id = None
    try:
        res = cj.create_order(order)
        print("SUCCESS — CJ accepted the order:")
        print(res)
        order_id = (res or {}).get("orderId") or (res or {}).get("orderNum")
    except CJError as e:
        print("CJ REJECTED THE ORDER — exact reason:")
        print(f"  {e}")
    except Exception as e:  # noqa: BLE001
        print(f"Unexpected error: {type(e).__name__}: {e}")

    # Verify the pay-from-balance endpoint. On an unfunded wallet this returns
    # an "insufficient balance" error, which safely confirms the endpoint and
    # field names are correct without moving any money.
    if args.pay and order_id:
        print(f"\nAttempting payBalance for order {order_id} …")
        if balance and balance > 0:
            print(f"  NOTE: wallet has ${balance:.2f} — this WILL spend real money.")
        try:
            pr = cj.pay_order(order_id)
            print("PAY SUCCESS — CJ order is paid and will be fulfilled:")
            print(f"  {pr}")
        except CJError as e:
            print("PAY REJECTED — exact reason (insufficient balance = endpoint OK):")
            print(f"  {e}")
        except Exception as e:  # noqa: BLE001
            print(f"Unexpected pay error: {type(e).__name__}: {e}")
    elif args.pay:
        print("\n(skipping pay — no order id was created)")


if __name__ == "__main__":
    main()
