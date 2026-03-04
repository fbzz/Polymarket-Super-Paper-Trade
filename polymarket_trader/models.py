from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

TradeDirection = Literal["YES", "NO"]


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


FeedEvent = PriceTick | MarketRotationTick


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

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    def unrealised(self, current_price: float) -> float:
        if self.direction == "YES":
            return (current_price - self.entry_price) * self.shares
        else:
            return (self.entry_price - current_price) * self.shares


@dataclass
class Portfolio:
    cash: float = 1000.0
    trades: list[Trade] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

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
    )


def portfolio_to_dict(p: Portfolio) -> dict:
    return {
        "cash": p.cash,
        "trades": [trade_to_dict(t) for t in p.trades],
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def portfolio_from_dict(d: dict) -> Portfolio:
    return Portfolio(
        cash=d["cash"],
        trades=[trade_from_dict(t) for t in d.get("trades", [])],
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )
