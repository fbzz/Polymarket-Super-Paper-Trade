from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from .exceptions import (
    InsufficientFundsError,
    MinimumOrderError,
    NoPriceAvailableError,
    TradeAlreadyClosedError,
    TradeNotFoundError,
)
from .fees import FeeModel, detect_fee_model
from .models import FeedEvent, MarketRotationTick, Portfolio, PriceTick, Trade
from .state import StateManager
from .utils import MarketClock, MarketSpec
from .websocket_feed import PolymarketFeed

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(
        self,
        market_id: str | None = None,
        *,
        asset: str | None = None,
        interval: str | None = None,
        initial_cash: float = 1000.0,
        state_file: str = "paper_trader_state.json",
        auto_close_on_rotation: bool = True,
        fee_model: FeeModel | None = None,
    ) -> None:
        if market_id:
            self._market_spec = MarketClock.parse(market_id)
        elif asset and interval:
            self._market_spec = MarketClock.current(asset, interval)
        else:
            raise ValueError("Provide either market_id or both asset and interval.")

        # Fee model: explicit > auto-detected from asset > no fees
        if fee_model is not None:
            self._fee_model = fee_model
        else:
            _asset = self._market_spec.asset
            self._fee_model = detect_fee_model(_asset)

        self._state = StateManager(state_file)
        self._portfolio = self._state.load()
        if not self._portfolio.trades and self._portfolio.cash == 1000.0:
            self._portfolio.cash = initial_cash
            self._state.save(self._portfolio)

        self.auto_close_on_rotation = auto_close_on_rotation
        self._latest_price: Optional[PriceTick] = None
        self._last_rotation: Optional[dict] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def market_id(self) -> str:
        return self._market_spec.market_id

    @property
    def latest_price(self) -> Optional[PriceTick]:
        return self._latest_price

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    @property
    def fee_model(self) -> FeeModel:
        return self._fee_model

    # ------------------------------------------------------------------
    # Trading API
    # ------------------------------------------------------------------

    def buy(
        self,
        direction: str,
        shares: float,
        price: float | None = None,
    ) -> Trade:
        if price is None:
            if self._latest_price is None:
                raise NoPriceAvailableError(
                    "No price available yet. Call stream() first or pass price= explicitly."
                )
            price = (
                self._latest_price.yes_price
                if direction == "YES"
                else self._latest_price.no_price
            )

        cost = shares * price
        if cost < 1.0:
            raise MinimumOrderError(
                f"Order total ${cost:.4f} is below the $1.00 minimum "
                f"({shares} shares × {price:.4f})."
            )

        fee = self._fee_model.taker_fee(shares, price)
        total = cost + fee

        if total > self._portfolio.cash:
            raise InsufficientFundsError(
                f"Need ${total:.4f} (${cost:.4f} + ${fee:.4f} fee) "
                f"but only ${self._portfolio.cash:.4f} available."
            )

        trade = Trade(
            id=str(uuid.uuid4()),
            market_id=self.market_id,
            direction=direction,  # type: ignore[arg-type]
            shares=shares,
            entry_price=price,
            entry_time=datetime.now(timezone.utc).isoformat(),
            entry_fee=fee,
        )
        self._portfolio.cash -= total
        self._portfolio.trades.append(trade)
        self._portfolio.updated_at = datetime.now(timezone.utc).isoformat()
        self._state.save(self._portfolio)
        logger.info(
            "BUY %s %s @ %.4f  cost %.4f  fee %.4f  total %.4f",
            direction, shares, price, cost, fee, total,
        )
        return trade

    def close(
        self,
        trade_id: str,
        price: float | None = None,
        *,
        maker: bool = False,
    ) -> Trade:
        """
        Close an open position.

        Parameters
        ----------
        trade_id : str
            ID of the trade to close.
        price : float | None
            Exit price. Uses latest_price if omitted.
        maker : bool
            If True, applies maker rebate instead of full taker fee on exit.
            Use this when you're closing with a resting limit order.
        """
        trade = next((t for t in self._portfolio.trades if t.id == trade_id), None)
        if trade is None:
            raise TradeNotFoundError(f"Trade {trade_id!r} not found.")
        if not trade.is_open:
            raise TradeAlreadyClosedError(f"Trade {trade_id!r} is already closed.")

        if price is None:
            if self._latest_price is None:
                raise NoPriceAvailableError(
                    "No price available. Pass price= explicitly."
                )
            price = (
                self._latest_price.yes_price
                if trade.direction == "YES"
                else self._latest_price.no_price
            )

        exit_fee = (
            self._fee_model.maker_fee(trade.shares, price)
            if maker
            else self._fee_model.taker_fee(trade.shares, price)
        )
        proceeds = trade.shares * price - exit_fee

        if trade.direction == "YES":
            pnl = (price - trade.entry_price) * trade.shares - trade.entry_fee - exit_fee
        else:
            pnl = (trade.entry_price - price) * trade.shares - trade.entry_fee - exit_fee

        trade.exit_price = price
        trade.exit_time = datetime.now(timezone.utc).isoformat()
        trade.exit_fee = exit_fee
        trade.pnl = pnl
        self._portfolio.cash += proceeds
        self._portfolio.updated_at = datetime.now(timezone.utc).isoformat()
        self._state.save(self._portfolio)
        logger.info(
            "CLOSE %s %s @ %.4f  fee %.4f  pnl %.4f",
            trade.direction, trade.shares, price, exit_fee, pnl,
        )
        return trade

    def close_all(self, price: float | None = None, *, maker: bool = False) -> list[Trade]:
        return [
            self.close(t.id, price=price, maker=maker)
            for t in list(self._portfolio.open_trades)
        ]

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream(self, on_tick: Callable) -> None:
        feed = PolymarketFeed(self._market_spec)
        async for event in feed.price_stream():
            if isinstance(event, PriceTick):
                self._latest_price = event
            elif isinstance(event, MarketRotationTick):
                logger.info(
                    "Market rotation: %s → %s",
                    event.old_market_id,
                    event.new_market_id,
                )
                if self.auto_close_on_rotation:
                    self._force_close_all(event)
                self._market_spec = MarketClock.parse(event.new_market_id)

            if asyncio.iscoroutinefunction(on_tick):
                await on_tick(event)
            else:
                on_tick(event)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _force_close_all(self, tick: MarketRotationTick) -> None:
        open_trades = list(self._portfolio.open_trades)
        if not open_trades:
            return

        lp = self._latest_price
        closed_ids = []
        for trade in open_trades:
            price = (lp.yes_price if trade.direction == "YES" else lp.no_price) if lp else trade.entry_price
            exit_fee = self._fee_model.taker_fee(trade.shares, price)
            proceeds = trade.shares * price - exit_fee

            if trade.direction == "YES":
                pnl = (price - trade.entry_price) * trade.shares - trade.entry_fee - exit_fee
            else:
                pnl = (trade.entry_price - price) * trade.shares - trade.entry_fee - exit_fee

            trade.exit_price = price
            trade.exit_time = datetime.now(timezone.utc).isoformat()
            trade.exit_fee = exit_fee
            trade.pnl = pnl
            trade.force_closed = True
            self._portfolio.cash += proceeds
            closed_ids.append((trade.id, pnl))

        self._portfolio.updated_at = datetime.now(timezone.utc).isoformat()
        self._state.save(self._portfolio)
        self._last_rotation = {
            "old_market_id": tick.old_market_id,
            "new_market_id": tick.new_market_id,
            "timestamp": tick.timestamp,
            "force_closed_trades": [{"id": tid, "pnl": pnl} for tid, pnl in closed_ids],
        }
        logger.warning(
            "Force-closed %d trade(s) on rotation: %s",
            len(closed_ids),
            closed_ids,
        )

    def summary(self) -> dict:
        lp = self._latest_price
        current_prices = {}
        if lp:
            current_prices[lp.market_id] = lp.yes_price

        s = self._portfolio.summary(current_prices)
        s["market_id"] = self.market_id
        s["latest_yes_price"] = lp.yes_price if lp else None
        s["latest_no_price"] = lp.no_price if lp else None
        s["last_rotation"] = self._last_rotation
        s["fee_model"] = {
            "fee_rate": self._fee_model.fee_rate,
            "exponent": self._fee_model.exponent,
            "maker_rebate": self._fee_model.maker_rebate,
        }
        return s
