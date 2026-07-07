"""
pricing.py — the single source of truth for how retail prices are computed.

Used by both the sync engine (to write products.json) and the server
(to re-validate a price before capturing payment). Keep the formula here
and nowhere else so the storefront, the sync job, and the checkout can
never disagree about what a product costs.

TRANSPARENT PRICE BUILD (sticker):

    sticker = cost
              × (1 + base_margin)     # your target profit over cost      (e.g. +20%)
              × (1 + coupon_buffer)   # headroom so a coupon doesn't       (e.g. +15%)
                                      #   eat into your margin
              × (1 + markup)          # extra markup the launch promo      (e.g. ×2 = +100%)
                                      #   is designed to cancel

The LAUNCH PROMO (a separate sitewide discount, handled in server.py) then
knocks a percentage off this sticker at display + checkout. So with the
defaults, a 50% promo brings the price back to cost × 1.20 × 1.15 = 1.38×
cost, and a 15% coupon on top uses up the coupon buffer.
"""

from dataclasses import dataclass
import math


@dataclass
class PricingConfig:
    base_margin: float = 0.20        # target profit over cost
    coupon_buffer: float = 0.15      # headroom for a coupon to consume
    markup: float = 1.00             # extra markup the launch promo cancels (100%)
    gateway_fee_rate: float = 0.03   # used to gross up pass-through shipping

    @classmethod
    def from_store(cls, store: dict) -> "PricingConfig":
        return cls(
            # `profit_target` kept as a fallback so older configs still load.
            base_margin=store.get("base_margin", store.get("profit_target", 0.20)),
            coupon_buffer=store.get("coupon_buffer", 0.15),
            markup=store.get("markup", 1.00),
            gateway_fee_rate=store.get("gateway_fee_rate", 0.03),
        )


def retail_price(cost: float, cfg: PricingConfig) -> float:
    """Sticker price = cost + profit margin + coupon buffer, then marked up.
    The launch promo (applied in the quote) discounts this at checkout."""
    sticker = cost * (1 + cfg.base_margin) * (1 + cfg.coupon_buffer) * (1 + cfg.markup)
    return math.ceil(sticker * 100) / 100   # ceil to the cent


def gross_up(amount: float, gateway_fee_rate: float = 0.03) -> float:
    """Gross up a pass-through cost (e.g. shipping) so the gateway fee on it
    doesn't come out of your pocket."""
    return math.ceil(amount / (1 - gateway_fee_rate) * 100) / 100


if __name__ == "__main__":
    cfg = PricingConfig()
    for c in (1.20, 9.78, 15.75, 31.20):
        r = retail_price(c, cfg)
        promo = round(r * 0.5, 2)              # after a 50% launch promo
        coup = round(promo * 0.85, 2)          # after a 15% coupon on top
        print(f"cost ${c:>6.2f} -> sticker ${r:>7.2f} | promo ${promo:>6.2f} "
              f"| +coupon ${coup:>6.2f} ({round((coup-c)/c*100)}% over cost)")
