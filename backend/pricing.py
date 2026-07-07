"""
pricing.py — the single source of truth for how retail prices are computed.

Used by both the sync engine (to write products.json) and the server
(to re-validate a price before capturing payment). Keep the formula here
and nowhere else so the storefront, the sync job, and the checkout can
never disagree about what a product costs.

GUARANTEED-MARGIN PRICE BUILD (the MSRP works BACKWARDS from the floor):

    Instead of marking a cost up and hoping the promos don't eat the profit,
    the sticker (MSRP) is *derived* from the profit you must keep and the
    deepest discount a customer could ever stack:

        floor   = cost * (1 + min_margin)          # what you must still net
        worst   = (1 - max_sale_pct) * (1 - max_coupon_pct)   # deepest stack
        sticker = ceil( floor / worst )

    Because the sticker is divided by the worst-case discount, ANY promotion
    up to that worst case leaves you at or above `min_margin`. A shallower
    promo (or no coupon) only makes your margin bigger — never smaller.

    Example (min_margin 10%, max_sale 50%, max_coupon 15%):
        worst   = 0.50 * 0.85 = 0.425
        sticker = cost * 1.10 / 0.425 = cost * 2.588
        - full price, no promo ....... +159% over cost
        - 50% launch promo, no coupon . +29% over cost
        - 50% promo + 15% coupon ...... +10% over cost   <- the floor, exactly

    To run a DEEPER promo later (say 60% off), just raise `max_sale_pct` to
    0.60 and every sticker automatically climbs so the 10% floor still holds.
"""

from dataclasses import dataclass
import math


@dataclass
class PricingConfig:
    min_margin: float = 0.10        # profit floor guaranteed AFTER the deepest promo stack
    max_sale_pct: float = 0.50      # deepest sitewide promo you would ever run
    max_coupon_pct: float = 0.15    # deepest coupon you would ever issue
    gateway_fee_rate: float = 0.03  # used to gross up pass-through shipping

    @classmethod
    def from_store(cls, store: dict) -> "PricingConfig":
        return cls(
            # `base_margin` kept as a fallback so older configs still load.
            min_margin=store.get("min_margin", store.get("base_margin", 0.10)),
            max_sale_pct=store.get("max_sale_pct", 0.50),
            max_coupon_pct=store.get("max_coupon_pct", 0.15),
            gateway_fee_rate=store.get("gateway_fee_rate", 0.03),
        )


def worst_case_discount(cfg: PricingConfig) -> float:
    """The deepest multiplier a customer can reach: promo AND coupon stacked."""
    return (1 - cfg.max_sale_pct) * (1 - cfg.max_coupon_pct)


def retail_price(cost: float, cfg: PricingConfig) -> float:
    """MSRP sized so the deepest promo + coupon stack still nets `min_margin`."""
    floor = cost * (1 + cfg.min_margin)          # profit we must keep
    sticker = floor / worst_case_discount(cfg)   # divide out the worst-case discount
    return math.ceil(sticker * 100) / 100        # ceil to the cent (rounds in your favor)


def gross_up(amount: float, gateway_fee_rate: float = 0.03) -> float:
    """Gross up a pass-through cost (e.g. shipping) so the gateway fee on it
    doesn't come out of your pocket."""
    return math.ceil(amount / (1 - gateway_fee_rate) * 100) / 100


if __name__ == "__main__":
    cfg = PricingConfig()
    worst = worst_case_discount(cfg)
    for c in (1.20, 9.78, 15.75, 31.20):
        r = retail_price(c, cfg)
        promo = round(r * (1 - cfg.max_sale_pct), 2)          # after the deepest promo
        both = round(promo * (1 - cfg.max_coupon_pct), 2)     # promo + coupon stacked
        print(f"cost ${c:>6.2f} -> MSRP ${r:>7.2f} | promo ${promo:>6.2f} "
              f"| +coupon ${both:>6.2f}  (floor {round((both-c)/c*100)}% over cost)")
