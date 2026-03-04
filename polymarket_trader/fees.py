"""
Polymarket fee models.

Formula (from https://docs.polymarket.com/trading/fees):
    fee = shares × price × fee_rate × (price × (1 - price))^exponent

Fees peak at 50% probability and fall to near-zero at market extremes.
Minimum charged: 0.0001 USDC — anything smaller rounds to zero.

Maker rebate: makers receive back a fraction of the taker fee they would
have paid. In paper trading every order is treated as a taker (crossing the
spread), so the full fee applies on entry. A configurable fraction is
returned on close to simulate maker rebate when closing with a limit order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_MIN_FEE = 0.0001   # USDC — fees below this round to zero


@dataclass(frozen=True)
class FeeModel:
    """
    Immutable fee configuration for a market type.

    Attributes:
        fee_rate:      Polymarket fee rate constant (e.g. 0.25 for crypto)
        exponent:      Shape exponent (e.g. 2 for crypto, 1 for sports)
        maker_rebate:  Fraction of the taker fee returned to makers (0–1)
    """
    fee_rate: float
    exponent: float
    maker_rebate: float = 0.0

    def taker_fee(self, shares: float, price: float) -> float:
        """Full taker fee for an order of *shares* at *price*."""
        raw = shares * price * self.fee_rate * (price * (1 - price)) ** self.exponent
        return 0.0 if raw < _MIN_FEE else round(raw, 6)

    def maker_fee(self, shares: float, price: float) -> float:
        """Net fee after maker rebate (used when closing a position)."""
        full = self.taker_fee(shares, price)
        return round(full * (1 - self.maker_rebate), 6)

    def effective_rate(self, price: float) -> float:
        """Effective fee as a fraction of notional value at a given price."""
        if price <= 0 or price >= 1:
            return 0.0
        return self.fee_rate * (price * (1 - price)) ** self.exponent


# ---------------------------------------------------------------------------
# Predefined models
# ---------------------------------------------------------------------------

#: Crypto markets (BTC, ETH, SOL, …) — max ~1.56 % at p=0.5
CRYPTO_FEES = FeeModel(fee_rate=0.25, exponent=2, maker_rebate=0.20)

#: Sports markets (NCAAB, Serie A, …) — max ~0.44 % at p=0.5
SPORTS_FEES = FeeModel(fee_rate=0.0175, exponent=1, maker_rebate=0.25)

#: No fees
NO_FEES = FeeModel(fee_rate=0.0, exponent=1, maker_rebate=0.0)

# Assets that attract crypto fees by default
_CRYPTO_ASSETS = {
    "btc", "eth", "sol", "xrp", "bnb", "doge", "avax",
    "link", "matic", "dot", "ada", "ltc", "atom", "near",
}


def detect_fee_model(asset: str) -> FeeModel:
    """Return the default FeeModel for a given asset slug."""
    return CRYPTO_FEES if asset.lower() in _CRYPTO_ASSETS else NO_FEES
