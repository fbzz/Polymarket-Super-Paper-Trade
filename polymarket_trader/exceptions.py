class PolymarketTraderError(Exception):
    """Base exception for polymarket_trader."""


class NoPriceAvailableError(PolymarketTraderError):
    """Raised when a trade is attempted but no price feed is available yet."""


class InsufficientFundsError(PolymarketTraderError):
    """Raised when a buy order exceeds available cash."""


class TradeNotFoundError(PolymarketTraderError):
    """Raised when a trade_id does not exist in the portfolio."""


class TradeAlreadyClosedError(PolymarketTraderError):
    """Raised when attempting to close an already-closed trade."""


class MinimumOrderError(PolymarketTraderError):
    """Raised when an order's total cost is below the $1.00 minimum."""


class MarketResolutionError(PolymarketTraderError):
    """Raised when market resolution or rotation fails."""
