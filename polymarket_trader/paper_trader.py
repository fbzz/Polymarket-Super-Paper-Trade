from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from .exceptions import (
    InsufficientFundsError,
    InsufficientLiquidityError,
    MinimumOrderError,
    NoPriceAvailableError,
    OrderNotFoundError,
    PostOnlyCancelledError,
    TradeAlreadyClosedError,
    TradeNotFoundError,
)
from .fees import FeeModel, detect_fee_model
from .models import (
    FeedEvent,
    MarketRotationTick,
    OrderFillEvent,
    PendingOrder,
    Portfolio,
    PriceTick,
    TimeInForce,
    Trade,
)
from .state import StateManager
from .utils import MarketClock, MarketSpec
from .websocket_feed import PolymarketFeed

logger = logging.getLogger(__name__)


import time as _time


def _fill_price(levels: list, shares: float) -> float | None:
    """Simulate a market-order fill by walking the order book.

    levels  — ask levels for a buy  (sorted price ascending, cheapest first)
              bid levels for a sell (sorted price descending, highest first)
    shares  — number of shares to fill

    Returns the VWAP fill price across consumed levels.
    If the book has less liquidity than requested, the remainder fills at the
    last available level (no partial refusal — matches market-order semantics).
    Returns None only when the book is completely empty.
    """
    if not levels:
        return None
    remaining = shares
    total_cost = 0.0
    last_price = levels[0].price
    for level in levels:
        if level.size <= 0:
            continue
        take = min(remaining, level.size)
        total_cost += take * level.price
        remaining -= take
        last_price = level.price
        if remaining <= 0:
            break
    if remaining > 0:
        # Insufficient depth — fill remainder at worst available price
        total_cost += remaining * last_price
    return total_cost / shares


def _fill_price_limited(
    levels: list,
    shares: float,
    limit_price: float,
    side: str,
) -> tuple[float, float | None]:
    """Walk order-book levels up to a limit price.

    side: "buy"  — only consume ask levels at price <= limit_price (ascending)
          "sell" — only consume bid levels at price >= limit_price (descending)

    Returns (filled_shares, avg_fill_price | None).
    avg_fill_price is None only when filled_shares == 0.
    """
    if not levels:
        return 0.0, None
    remaining = shares
    total_cost = 0.0
    for level in levels:
        if level.size <= 0:
            continue
        if side == "buy" and level.price > limit_price:
            break
        if side == "sell" and level.price < limit_price:
            break
        take = min(remaining, level.size)
        total_cost += take * level.price
        remaining -= take
        if remaining <= 0:
            break
    filled = shares - remaining
    avg_price = (total_cost / filled) if filled > 0 else None
    return filled, avg_price


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
        *,
        tif: TimeInForce | str = TimeInForce.MARKET,
        post_only: bool = False,
        expiration: float | None = None,
    ) -> Trade | PendingOrder:
        tif = TimeInForce(tif)

        if tif == TimeInForce.MARKET:
            return self._buy_market(direction, shares, price)
        if tif == TimeInForce.FOK:
            return self._buy_fok(direction, shares, price)
        if tif == TimeInForce.FAK:
            return self._buy_fak(direction, shares, price)
        # GTC / GTD
        if price is None:
            raise ValueError("price= is required for GTC/GTD orders.")
        return self._buy_limit(direction, shares, price, tif, post_only, expiration)

    def _buy_market(self, direction: str, shares: float, price: float | None) -> Trade:
        if price is None:
            if self._latest_price is None:
                raise NoPriceAvailableError(
                    "No price available yet. Call stream() first or pass price= explicitly."
                )
            ob = self._latest_price.order_book
            levels = ob.yes_asks if direction == "YES" else ob.no_asks
            price = _fill_price(levels, shares)
            if price is None:
                price = (
                    self._latest_price.yes_price
                    if direction == "YES"
                    else self._latest_price.no_price
                )
        return self._execute_buy(direction, shares, price, maker=False)

    def _buy_fok(self, direction: str, shares: float, price: float | None) -> Trade:
        if self._latest_price is None:
            raise NoPriceAvailableError(
                "No price available yet. Call stream() first or pass price= explicitly."
            )
        ob = self._latest_price.order_book
        levels = ob.yes_asks if direction == "YES" else ob.no_asks
        limit = price if price is not None else float("inf")
        filled, avg_price = _fill_price_limited(levels, shares, limit, "buy")
        if filled < shares:
            raise InsufficientLiquidityError(
                f"FOK cannot fill {shares} shares at limit {price}; "
                f"only {filled:.4f} available."
            )
        fill_price = avg_price if avg_price is not None else (price or 0.0)
        return self._execute_buy(direction, shares, fill_price, maker=False)

    def _buy_fak(self, direction: str, shares: float, price: float | None) -> Trade:
        if self._latest_price is None:
            raise NoPriceAvailableError(
                "No price available yet. Call stream() first or pass price= explicitly."
            )
        ob = self._latest_price.order_book
        levels = ob.yes_asks if direction == "YES" else ob.no_asks
        limit = price if price is not None else float("inf")
        filled, avg_price = _fill_price_limited(levels, shares, limit, "buy")
        if filled == 0 or avg_price is None:
            raise InsufficientLiquidityError(
                f"FAK: no liquidity available at limit {price}."
            )
        return self._execute_buy(direction, filled, avg_price, maker=False)

    def _buy_limit(
        self,
        direction: str,
        shares: float,
        price: float,
        tif: TimeInForce,
        post_only: bool,
        expiration: float | None,
    ) -> Trade | PendingOrder:
        # Check if immediately fillable (crosses the spread)
        if self._latest_price is not None:
            ob = self._latest_price.order_book
            levels = ob.yes_asks if direction == "YES" else ob.no_asks
            best_ask = levels[0].price if levels else None
            if best_ask is not None and best_ask <= price:
                if post_only:
                    raise PostOnlyCancelledError(
                        f"Post-only order would cross spread: best_ask={best_ask:.4f} <= limit={price:.4f}."
                    )
                fill_price = _fill_price(levels, shares) or price
                return self._execute_buy(direction, shares, fill_price, maker=False)

        # Resting order — validate and reserve cash
        cost_reserved = shares * price
        if cost_reserved < 1.0:
            raise MinimumOrderError(
                f"Order total ${cost_reserved:.4f} is below the $1.00 minimum."
            )
        if cost_reserved > self._portfolio.cash:
            raise InsufficientFundsError(
                f"Need ${cost_reserved:.4f} to reserve but only "
                f"${self._portfolio.cash:.4f} available."
            )

        order = PendingOrder(
            id=str(uuid.uuid4()),
            market_id=self.market_id,
            direction=direction,  # type: ignore[arg-type]
            shares=shares,
            limit_price=price,
            tif=tif,
            post_only=post_only,
            created_at=datetime.now(timezone.utc).isoformat(),
            expiration=expiration if tif == TimeInForce.GTD else None,
            close_trade_id=None,
        )
        self._portfolio.cash -= cost_reserved
        self._portfolio.pending_orders.append(order)
        self._portfolio.updated_at = datetime.now(timezone.utc).isoformat()
        self._state.save(self._portfolio)
        logger.info(
            "PENDING BUY %s %s @ %.4f  reserved %.4f  tif=%s",
            direction, shares, price, cost_reserved, tif.value,
        )
        return order

    def _execute_buy(self, direction: str, shares: float, price: float, maker: bool) -> Trade:
        """Common path that debits cash and creates a Trade."""
        cost = shares * price
        if cost < 1.0:
            raise MinimumOrderError(
                f"Order total ${cost:.4f} is below the $1.00 minimum "
                f"({shares} shares × {price:.4f})."
            )
        fee = (
            self._fee_model.maker_fee(shares, price)
            if maker
            else self._fee_model.taker_fee(shares, price)
        )
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
        tif: TimeInForce | str = TimeInForce.MARKET,
        post_only: bool = False,
        expiration: float | None = None,
    ) -> Trade | PendingOrder:
        tif = TimeInForce(tif)

        trade = next((t for t in self._portfolio.trades if t.id == trade_id), None)
        if trade is None:
            raise TradeNotFoundError(f"Trade {trade_id!r} not found.")
        if not trade.is_open:
            raise TradeAlreadyClosedError(f"Trade {trade_id!r} is already closed.")

        if tif == TimeInForce.MARKET:
            return self._close_market(trade, price, maker=maker)
        if tif == TimeInForce.FOK:
            return self._close_fok(trade, price)
        if tif == TimeInForce.FAK:
            return self._close_fak(trade, price)
        # GTC / GTD
        if price is None:
            raise ValueError("price= is required for GTC/GTD close orders.")
        return self._close_limit(trade, price, tif, post_only, expiration)

    def _close_market(self, trade: Trade, price: float | None, *, maker: bool) -> Trade:
        if price is None:
            if self._latest_price is None:
                raise NoPriceAvailableError(
                    "No price available. Pass price= explicitly."
                )
            ob = self._latest_price.order_book
            levels = ob.yes_bids if trade.direction == "YES" else ob.no_bids
            price = _fill_price(levels, trade.shares)
            if price is None:
                price = (
                    self._latest_price.yes_price
                    if trade.direction == "YES"
                    else self._latest_price.no_price
                )
        return self._execute_close(trade, price, maker=maker)

    def _close_fok(self, trade: Trade, price: float | None) -> Trade:
        if self._latest_price is None:
            raise NoPriceAvailableError("No price available. Pass price= explicitly.")
        ob = self._latest_price.order_book
        levels = ob.yes_bids if trade.direction == "YES" else ob.no_bids
        limit = price if price is not None else 0.0
        filled, avg_price = _fill_price_limited(levels, trade.shares, limit, "sell")
        if filled < trade.shares:
            raise InsufficientLiquidityError(
                f"FOK close cannot fill {trade.shares} shares at limit {price}; "
                f"only {filled:.4f} available."
            )
        fill_price = avg_price if avg_price is not None else (price or trade.entry_price)
        return self._execute_close(trade, fill_price, maker=False)

    def _close_fak(self, trade: Trade, price: float | None) -> Trade:
        if self._latest_price is None:
            raise NoPriceAvailableError("No price available. Pass price= explicitly.")
        ob = self._latest_price.order_book
        levels = ob.yes_bids if trade.direction == "YES" else ob.no_bids
        limit = price if price is not None else 0.0
        filled, avg_price = _fill_price_limited(levels, trade.shares, limit, "sell")
        if filled == 0 or avg_price is None:
            raise InsufficientLiquidityError(
                f"FAK close: no liquidity available at limit {price}."
            )
        # Partial close — update trade shares then execute
        trade.shares = filled
        return self._execute_close(trade, avg_price, maker=False)

    def _close_limit(
        self,
        trade: Trade,
        price: float,
        tif: TimeInForce,
        post_only: bool,
        expiration: float | None,
    ) -> Trade | PendingOrder:
        # Check if immediately fillable (crosses the spread)
        if self._latest_price is not None:
            ob = self._latest_price.order_book
            levels = ob.yes_bids if trade.direction == "YES" else ob.no_bids
            best_bid = levels[0].price if levels else None
            if best_bid is not None and best_bid >= price:
                if post_only:
                    raise PostOnlyCancelledError(
                        f"Post-only close would cross spread: best_bid={best_bid:.4f} >= limit={price:.4f}."
                    )
                fill_price = _fill_price(levels, trade.shares) or price
                return self._execute_close(trade, fill_price, maker=False)

        # Resting close order — no cash reservation needed
        order = PendingOrder(
            id=str(uuid.uuid4()),
            market_id=self.market_id,
            direction=trade.direction,
            shares=trade.shares,
            limit_price=price,
            tif=tif,
            post_only=post_only,
            created_at=datetime.now(timezone.utc).isoformat(),
            expiration=expiration if tif == TimeInForce.GTD else None,
            close_trade_id=trade.id,
        )
        self._portfolio.pending_orders.append(order)
        self._portfolio.updated_at = datetime.now(timezone.utc).isoformat()
        self._state.save(self._portfolio)
        logger.info(
            "PENDING CLOSE %s %s @ %.4f  tif=%s",
            trade.direction, trade.shares, price, tif.value,
        )
        return order

    def _execute_close(self, trade: Trade, price: float, *, maker: bool) -> Trade:
        """Common path that credits cash and finalises a Trade."""
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

    def cancel_order(self, order_id: str) -> PendingOrder:
        """Cancel a pending GTC/GTD order and release any reserved cash."""
        order = next(
            (o for o in self._portfolio.pending_orders if o.id == order_id), None
        )
        if order is None:
            raise OrderNotFoundError(f"Order {order_id!r} not found.")
        self._portfolio.pending_orders.remove(order)
        # Release reserved cash for buy orders
        if order.close_trade_id is None:
            self._portfolio.cash += order.shares * order.limit_price
        self._portfolio.updated_at = datetime.now(timezone.utc).isoformat()
        self._state.save(self._portfolio)
        logger.info("CANCEL order %s  released %.4f", order_id, order.shares * order.limit_price)
        return order

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
                fill_events = self._check_pending_orders(event)
                # Yield fill events before on_tick so the callback can react
                for fill_event in fill_events:
                    if asyncio.iscoroutinefunction(on_tick):
                        await on_tick(fill_event)
                    else:
                        on_tick(fill_event)
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

    def _fill_pending_buy(self, order: PendingOrder, maker: bool) -> Trade:
        """Execute a resting buy order that has just been triggered."""
        fill_price = order.limit_price
        fee = (
            self._fee_model.maker_fee(order.shares, fill_price)
            if maker
            else self._fee_model.taker_fee(order.shares, fill_price)
        )
        # Cash was already reserved (= shares × limit_price); deduct fee on top
        self._portfolio.cash -= fee
        trade = Trade(
            id=str(uuid.uuid4()),
            market_id=order.market_id,
            direction=order.direction,
            shares=order.shares,
            entry_price=fill_price,
            entry_time=datetime.now(timezone.utc).isoformat(),
            entry_fee=fee,
        )
        self._portfolio.trades.append(trade)
        self._portfolio.pending_orders.remove(order)
        self._portfolio.updated_at = datetime.now(timezone.utc).isoformat()
        self._state.save(self._portfolio)
        logger.info(
            "FILL pending BUY %s %s @ %.4f  fee %.4f  (maker=%s)",
            order.direction, order.shares, fill_price, fee, maker,
        )
        return trade

    def _fill_pending_close(self, order: PendingOrder, maker: bool) -> Trade:
        """Execute a resting close order that has just been triggered."""
        trade = next(
            (t for t in self._portfolio.trades if t.id == order.close_trade_id), None
        )
        if trade is None or not trade.is_open:
            self._portfolio.pending_orders.remove(order)
            self._state.save(self._portfolio)
            raise TradeNotFoundError(
                f"Trade {order.close_trade_id!r} for pending close order not found or already closed."
            )
        self._portfolio.pending_orders.remove(order)
        return self._execute_close(trade, order.limit_price, maker=maker)

    def _check_pending_orders(self, tick: PriceTick) -> list[OrderFillEvent]:
        """Evaluate all pending orders against current tick; return fill events."""
        now = _time.time()
        events: list[OrderFillEvent] = []
        for order in list(self._portfolio.pending_orders):
            # GTD expiry
            if order.tif == TimeInForce.GTD and order.expiration is not None and now >= order.expiration:
                logger.info("GTD order %s expired.", order.id)
                self._portfolio.pending_orders.remove(order)
                if order.close_trade_id is None:
                    # Release reserved cash
                    self._portfolio.cash += order.shares * order.limit_price
                self._portfolio.updated_at = datetime.now(timezone.utc).isoformat()
                self._state.save(self._portfolio)
                continue

            ob = tick.order_book
            if order.close_trade_id is None:
                # Buy order — fill when best_ask <= limit_price
                levels = ob.yes_asks if order.direction == "YES" else ob.no_asks
                best_ask = levels[0].price if levels else None
                if best_ask is not None and best_ask <= order.limit_price:
                    try:
                        trade = self._fill_pending_buy(order, maker=True)
                        events.append(OrderFillEvent(
                            order_id=order.id,
                            trade=trade,
                            timestamp=tick.timestamp,
                        ))
                    except Exception as exc:
                        logger.warning("Failed to fill pending buy %s: %s", order.id, exc)
            else:
                # Close order — fill when best_bid >= limit_price
                levels = ob.yes_bids if order.direction == "YES" else ob.no_bids
                best_bid = levels[0].price if levels else None
                if best_bid is not None and best_bid >= order.limit_price:
                    try:
                        trade = self._fill_pending_close(order, maker=True)
                        events.append(OrderFillEvent(
                            order_id=order.id,
                            trade=trade,
                            timestamp=tick.timestamp,
                        ))
                    except Exception as exc:
                        logger.warning("Failed to fill pending close %s: %s", order.id, exc)
        return events

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
        s["pending_orders"] = len(self._portfolio.pending_orders)
        s["reserved_cash"] = self._portfolio.reserved_cash
        s["fee_model"] = {
            "fee_rate": self._fee_model.fee_rate,
            "exponent": self._fee_model.exponent,
            "maker_rebate": self._fee_model.maker_rebate,
        }
        return s
