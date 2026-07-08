"""
pricing.py — the single source of truth for how retail prices are computed.

Used by both the sync engine (to write products.json) and the server
(to re-validate a price before capturing payment). Keep the formula here
and nowhere else so the storefront, the sync job, and the checkout can
never disagree about what a product costs.

HONEST-PRICE BUILD, WITH A MINIMUM-PROFIT FLOOR (no fictitious "was" price):

    The listed price IS the price. There is no inflated MSRP shown as a
    struck-through "regular price" (that would be a fictitious former price —
    FTC 16 CFR 233 / California Bus & Prof Code §17501). The only discount is
    a genuine per-customer coupon (WELCOME15, 15% off a first order).

    Two constraints set the sticker; we take whichever is higher:

    1) Percentage floor — after the coupon you still net `min_margin` over cost:
           base = cost * (1 + min_margin) / (1 - max_coupon_pct)

    2) Absolute floor — after the coupon AND the card fee (pct + fixed) you
       still clear at least `min_net_profit` in real dollars:
           net = P*(1-coupon)*(1-fee_rate) - fee_fixed - cost >= min_net_profit
       solved for P:
           floor = (min_net_profit + cost + fee_fixed)
                   / ((1 - max_coupon_pct) * (1 - fee_rate))

           sticker = ceil( max(base, floor) )

    The absolute floor is what stops the flat card fee + coupon from eating a
    cheap item to ~zero: every order clears real money. It raises inexpensive
    items the most; genuinely high-cost items are governed by the % build.
"""

from dataclasses import dataclass
import math


@dataclass
class PricingConfig:
    min_margin: float = 0.10        # profit floor (%) guaranteed after the coupon
    max_coupon_pct: float = 0.15    # deepest coupon you would ever issue (WELCOME15)
    min_net_profit: float = 3.00    # guaranteed $ profit/order after coupon + card fee
    gateway_fee_rate: float = 0.029 # card processor % fee (Stripe 2.9%)
    gateway_fixed: float = 0.30     # card processor fixed fee per charge
    max_sale_pct: float = 0.0       # reporting only — NOT applied to the sticker

    @classmethod
    def from_store(cls, store: dict) -> "PricingConfig":
        return cls(
            # `base_margin` kept as a fallback so older configs still load.
            min_margin=store.get("min_margin", store.get("base_margin", 0.10)),
            max_coupon_pct=store.get("max_coupon_pct", 0.15),
            min_net_profit=store.get("min_net_profit", 3.00),
            gateway_fee_rate=store.get("gateway_fee_rate", 0.029),
            gateway_fixed=store.get("gateway_fixed", 0.30),
            max_sale_pct=store.get("max_sale_pct", 0.0),
        )


def retail_price(cost: float, cfg: PricingConfig) -> float:
    """Honest list price = the higher of the % floor and the absolute-$ floor."""
    base = cost * (1 + cfg.min_margin) / (1 - cfg.max_coupon_pct)
    denom = (1 - cfg.max_coupon_pct) * (1 - cfg.gateway_fee_rate)
    floor = (cfg.min_net_profit + cost + cfg.gateway_fixed) / denom
    return math.ceil(max(base, floor) * 100) / 100


def gross_up(amount: float, gateway_fee_rate: float = 0.029) -> float:
    """Gross up a pass-through cost (e.g. shipping) so the gateway fee on it
    doesn't come out of your pocket."""
    return math.ceil(amount / (1 - gateway_fee_rate) * 100) / 100


if __name__ == "__main__":
    cfg = PricingConfig()
    for c in (1.20, 9.78, 15.75, 31.20, 55.00):
        r = retail_price(c, cfg)
        coup = round(r * (1 - cfg.max_coupon_pct), 2)                       # after coupon
        net = round(coup * (1 - cfg.gateway_fee_rate) - cfg.gateway_fixed - c, 2)  # after fee
        print(f"cost ${c:>6.2f} -> list ${r:>7.2f} | after coupon ${coup:>7.2f} "
              f"| net after fee ${net:>5.2f}")
