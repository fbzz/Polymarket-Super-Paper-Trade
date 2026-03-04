from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

TradeDirection = Literal["YES", "NO"]


class TimeInForce(str, Enum):
    MARKET = "MARKET"  # default — VWAP fill at market
    GTC    = "GTC"     # resting limit, no expiry
    GTD    = "GTD"     # resting limit, auto-expires at unix timestamp
    FOK    = "FOK"     # immediate, full fill or raise InsufficientLiquidityError
    FAK    = "FAK"     # immediate, partial fill OK, cancel the rest


@dataclass
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    yes_bids: list[Level]
    yes_asks: list[Level]
    no_bids: list[Level]
    no_asks: list[Level]


@dataclass
class PriceTick:
    market_id: str
    yes_price: float
    no_price: float
    timestamp: str
    order_book: OrderBook


@dataclass
class MarketRotationTick:
    old_market_id: str
    new_market_id: str
    timestamp: str


@dataclass(slots=True)
class PendingOrder:
    id: str
    market_id: str
    direction: TradeDirection
    shares: float
    limit_price: float
    tif: TimeInForce
    post_only: bool
    created_at: str
    expiration: Optional[float]      # unix ts — GTD only, else None
    close_trade_id: Optional[str]    # set for close orders; None for buys


@dataclass
class OrderFillEvent:
    order_id: str
    trade: "Trade"    # Trade created (buy) or updated (close)
    timestamp: str


FeedEvent = PriceTick | MarketRotationTick | OrderFillEvent


@dataclass(slots=True)
class Trade:
    id: str
    market_id: str
    direction: TradeDirection
    shares: float
    entry_price: float
    entry_time: str
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl: Optional[float] = None
    force_closed: bool = False
    entry_fee: float = 0.0   # taker fee paid on open
    exit_fee: float = 0.0    # taker/maker fee paid on close

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def total_fees(self) -> float:
        return self.entry_fee + self.exit_fee

    def unrealised(self, current_price: float) -> float:
        """Price PnL minus entry fee (exit fee not yet known)."""
        if self.direction == "YES":
            return (current_price - self.entry_price) * self.shares - self.entry_fee
        else:
            return (self.entry_price - current_price) * self.shares - self.entry_fee


@dataclass
class Portfolio:
    cash: float = 1000.0
    trades: list[Trade] = field(default_factory=list)
    pending_orders: list[PendingOrder] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def reserved_cash(self) -> float:
        """Cash reserved by open GTC/GTD buy orders."""
        return sum(o.shares * o.limit_price for o in self.pending_orders if o.close_trade_id is None)

    @property
    def open_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.is_open]

    @property
    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades if not t.is_open]

    @property
    def unrealised_pnl(self) -> float:
        return 0.0  # requires current prices; use summary()

    @property
    def realised_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades if t.pnl is not None)

    @property
    def total_pnl(self) -> float:
        return self.realised_pnl  # unrealised requires prices

    @property
    def win_rate(self) -> Optional[float]:
        closed = self.closed_trades
        if not closed:
            return None
        wins = sum(1 for t in closed if t.pnl is not None and t.pnl > 0)
        return wins / len(closed)

    def summary(self, current_prices: dict[str, float] | None = None) -> dict:
        current_prices = current_prices or {}
        unrealised = sum(
            t.unrealised(current_prices[t.market_id])
            for t in self.open_trades
            if t.market_id in current_prices
        )
        return {
            "cash": self.cash,
            "open_trades": len(self.open_trades),
            "closed_trades": len(self.closed_trades),
            "realised_pnl": self.realised_pnl,
            "unrealised_pnl": unrealised,
            "total_pnl": self.realised_pnl + unrealised,
            "win_rate": self.win_rate,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# --- Serialisation helpers ---

def trade_to_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "market_id": t.market_id,
        "direction": t.direction,
        "shares": t.shares,
        "entry_price": t.entry_price,
        "entry_time": t.entry_time,
        "exit_price": t.exit_price,
        "exit_time": t.exit_time,
        "pnl": t.pnl,
        "force_closed": t.force_closed,
        "entry_fee": t.entry_fee,
        "exit_fee": t.exit_fee,
    }


def trade_from_dict(d: dict) -> Trade:
    return Trade(
        id=d["id"],
        market_id=d["market_id"],
        direction=d["direction"],
        shares=d["shares"],
        entry_price=d["entry_price"],
        entry_time=d["entry_time"],
        exit_price=d.get("exit_price"),
        exit_time=d.get("exit_time"),
        pnl=d.get("pnl"),
        force_closed=d.get("force_closed", False),
        entry_fee=d.get("entry_fee", 0.0),
        exit_fee=d.get("exit_fee", 0.0),
    )


def order_to_dict(o: PendingOrder) -> dict:
    return {
        "id": o.id,
        "market_id": o.market_id,
        "direction": o.direction,
        "shares": o.shares,
        "limit_price": o.limit_price,
        "tif": o.tif.value,
        "post_only": o.post_only,
        "created_at": o.created_at,
        "expiration": o.expiration,
        "close_trade_id": o.close_trade_id,
    }


def order_from_dict(d: dict) -> PendingOrder:
    return PendingOrder(
        id=d["id"],
        market_id=d["market_id"],
        direction=d["direction"],
        shares=d["shares"],
        limit_price=d["limit_price"],
        tif=TimeInForce(d["tif"]),
        post_only=d["post_only"],
        created_at=d["created_at"],
        expiration=d.get("expiration"),
        close_trade_id=d.get("close_trade_id"),
    )


def portfolio_to_dict(p: Portfolio) -> dict:
    return {
        "cash": p.cash,
        "trades": [trade_to_dict(t) for t in p.trades],
        "pending_orders": [order_to_dict(o) for o in p.pending_orders],
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def portfolio_from_dict(d: dict) -> Portfolio:
    return Portfolio(
        cash=d["cash"],
        trades=[trade_from_dict(t) for t in d.get("trades", [])],
        pending_orders=[order_from_dict(o) for o in d.get("pending_orders", [])],
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )
