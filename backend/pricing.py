"""
pricing.py — the single source of truth for how retail prices are computed.

Used by both the sync engine (to write products.json) and the server
(to re-validate a price before capturing payment). Keep the formula here
and nowhere else so the storefront, the sync job, and the checkout can
never disagree about what a product costs.

RISK-ADJUSTED MODEL ("profitable in aggregate"):

    retail = max(
        # percentage path — profit target + pre-funded loss provision,
        # grossed up so the gateway fee (taken on the FINAL price) is covered
        cost * (1 + profit_target + loss_provision_rate) / (1 - gateway_fee_rate),

        # absolute floor — guarantees a DOLLAR cushion even on near-free items
        (cost + min_profit_per_unit + expected_chargeback_rate * chargeback_fee)
            / (1 - gateway_fee_rate)
    )

This makes the STORE profitable across all orders. It does NOT make any
single chargeback impossible to lose on — nothing can. Keep chargeback_rate
low with fraud screening and fast/tracked shipping.
"""

from dataclasses import dataclass
import math


@dataclass
class PricingConfig:
    gateway_fee_rate: float = 0.03
    profit_target: float = 0.15
    loss_provision_rate: float = 0.08
    min_profit_per_unit: float = 7.00
    expected_return_rate: float = 0.06        # informational / reporting
    expected_chargeback_rate: float = 0.01
    chargeback_fee: float = 20.00

    @classmethod
    def from_store(cls, store: dict) -> "PricingConfig":
        return cls(
            gateway_fee_rate=store.get("gateway_fee_rate", 0.03),
            profit_target=store.get("profit_target", 0.15),
            loss_provision_rate=store.get("loss_provision_rate", 0.08),
            min_profit_per_unit=store.get("min_profit_per_unit", 7.00),
            expected_return_rate=store.get("expected_return_rate", 0.06),
            expected_chargeback_rate=store.get("expected_chargeback_rate", 0.01),
            chargeback_fee=store.get("chargeback_fee", 20.00),
        )


def retail_price(cost: float, cfg: PricingConfig) -> float:
    """Risk-adjusted retail price for a given supplier cost."""
    pct = cost * (1 + cfg.profit_target + cfg.loss_provision_rate) / (1 - cfg.gateway_fee_rate)
    floor = (cost + cfg.min_profit_per_unit
             + cfg.expected_chargeback_rate * cfg.chargeback_fee) / (1 - cfg.gateway_fee_rate)
    # ceil to the cent so rounding never dips below the floor
    return math.ceil(max(pct, floor) * 100) / 100


def gross_up(amount: float, gateway_fee_rate: float = 0.03) -> float:
    """Gross up a pass-through cost (e.g. shipping) so the gateway fee on it
    doesn't come out of your pocket."""
    return math.ceil(amount / (1 - gateway_fee_rate) * 100) / 100


if __name__ == "__main__":
    cfg = PricingConfig()
    for c in (1.20, 12.40, 15.75, 22.00, 38.50):
        r = retail_price(c, cfg)
        print(f"cost ${c:>6.2f} -> retail ${r:>6.2f}  (gross cushion ${r - c:>5.2f})")
