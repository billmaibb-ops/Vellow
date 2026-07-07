"""
pricing.py — the single source of truth for how retail prices are computed.

Used by both the sync engine (to write products.json) and the server
(to re-validate a price before capturing payment). Keep the formula here
and nowhere else so the storefront, the sync job, and the checkout can
never disagree about what a product costs.

HONEST-PRICE BUILD (no fictitious "was" price):

    The listed price IS the price. There is no inflated MSRP shown as a
    struck-through "regular price" — that would be a fictitious former price
    (FTC 16 CFR 233 / California Bus & Prof Code §17501) and reads as a scam.

    The only discount is a genuine, per-customer coupon (e.g. WELCOME15, 15%
    off a first order). The sticker is sized so that even after that coupon
    you still net `min_margin` over cost:

        floor   = cost * (1 + min_margin)        # profit you must keep
        sticker = ceil( floor / (1 - max_coupon_pct) )

    Example (min_margin 10%, max_coupon 15%):
        sticker = cost * 1.10 / 0.85 = cost * 1.294
        - list price ............... +29% over cost
        - after 15% WELCOME coupon .. +10% over cost   <- the floor, exactly

    NOTE: `max_sale_pct` is retained in the config for reporting only and is
    NOT applied to the sticker. Run real, time-boxed sales off this honest
    list price if you want urgency — never a permanent sitewide markdown.
"""

from dataclasses import dataclass
import math


@dataclass
class PricingConfig:
    min_margin: float = 0.10        # profit floor guaranteed after the coupon
    max_coupon_pct: float = 0.15    # deepest coupon you would ever issue (e.g. WELCOME15)
    max_sale_pct: float = 0.0       # reporting only — NOT applied to the sticker
    gateway_fee_rate: float = 0.03  # used to gross up pass-through shipping

    @classmethod
    def from_store(cls, store: dict) -> "PricingConfig":
        return cls(
            # `base_margin` kept as a fallback so older configs still load.
            min_margin=store.get("min_margin", store.get("base_margin", 0.10)),
            max_coupon_pct=store.get("max_coupon_pct", 0.15),
            max_sale_pct=store.get("max_sale_pct", 0.0),
            gateway_fee_rate=store.get("gateway_fee_rate", 0.03),
        )


def retail_price(cost: float, cfg: PricingConfig) -> float:
    """Honest list price, sized so the WELCOME coupon still nets `min_margin`."""
    floor = cost * (1 + cfg.min_margin)          # profit we must keep
    sticker = floor / (1 - cfg.max_coupon_pct)   # so a max coupon lands on the floor
    return math.ceil(sticker * 100) / 100        # ceil to the cent (rounds in your favor)


def gross_up(amount: float, gateway_fee_rate: float = 0.03) -> float:
    """Gross up a pass-through cost (e.g. shipping) so the gateway fee on it
    doesn't come out of your pocket."""
    return math.ceil(amount / (1 - gateway_fee_rate) * 100) / 100


if __name__ == "__main__":
    cfg = PricingConfig()
    for c in (1.20, 9.78, 15.75, 31.20):
        r = retail_price(c, cfg)
        coup = round(r * (1 - cfg.max_coupon_pct), 2)     # after the WELCOME coupon
        print(f"cost ${c:>6.2f} -> list ${r:>7.2f} | after coupon ${coup:>6.2f}  "
              f"(floor {round((coup-c)/c*100)}% over cost)")
