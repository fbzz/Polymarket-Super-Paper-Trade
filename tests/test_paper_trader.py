"""Unit tests for polymarket_trader — no network calls."""

from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from polymarket_trader import (
    CRYPTO_FEES,
    NO_FEES,
    SPORTS_FEES,
    InsufficientFundsError,
    InsufficientLiquidityError,
    MinimumOrderError,
    NoPriceAvailableError,
    OrderNotFoundError,
    PaperTrader,
    PostOnlyCancelledError,
    TradeAlreadyClosedError,
    TradeNotFoundError,
)
from polymarket_trader.fees import FeeModel, detect_fee_model
from polymarket_trader.models import (
    Level,
    OrderBook,
    OrderFillEvent,
    PendingOrder,
    Portfolio,
    PriceTick,
    TimeInForce,
    Trade,
    order_from_dict,
    order_to_dict,
    portfolio_from_dict,
    portfolio_to_dict,
    trade_from_dict,
    trade_to_dict,
)
from polymarket_trader.utils import INTERVAL_SECONDS, MarketClock, MarketSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_tick(
    yes=0.6,
    no=0.4,
    market_id="btc-updown-5m-9999999999",
    yes_asks: list[Level] | None = None,
    yes_bids: list[Level] | None = None,
    no_asks: list[Level] | None = None,
    no_bids: list[Level] | None = None,
) -> PriceTick:
    return PriceTick(
        market_id=market_id,
        yes_price=yes,
        no_price=no,
        timestamp="2026-01-01T00:00:00+00:00",
        order_book=OrderBook(
            yes_bids=yes_bids or [],
            yes_asks=yes_asks or [],
            no_bids=no_bids or [],
            no_asks=no_asks or [],
        ),
    )


def fresh_trader(**kwargs) -> PaperTrader:
    """PaperTrader backed by a fresh in-memory portfolio (no disk I/O).

    Defaults to NO_FEES so that PnL assertions stay simple. Pass
    fee_model=CRYPTO_FEES (or another FeeModel) to test fee behaviour.
    """
    future_ts = int(time.time()) + 3600
    market_id = f"btc-updown-5m-{future_ts}"
    with patch("polymarket_trader.paper_trader.StateManager") as MockSM:
        sm_instance = MagicMock()
        sm_instance.load.return_value = Portfolio(cash=kwargs.pop("cash", 1000.0))
        MockSM.return_value = sm_instance
        trader = PaperTrader(market_id=market_id, **kwargs)
        trader._state = sm_instance
    return trader


# ---------------------------------------------------------------------------
# utils.py
# ---------------------------------------------------------------------------


class TestMarketSpec:
    def test_market_id_roundtrip(self):
        spec = MarketSpec("btc", "5m", 1700000000)
        assert spec.market_id == "btc-updown-5m-1700000000"

    def test_interval_seconds(self):
        assert MarketSpec("btc", "5m", 0).interval_seconds == 300
        assert MarketSpec("btc", "15m", 0).interval_seconds == 900
        assert MarketSpec("btc", "1h", 0).interval_seconds == 3600
        assert MarketSpec("btc", "1d", 0).interval_seconds == 86400

    def test_next(self):
        spec = MarketSpec("btc", "5m", 1700000300)
        assert spec.next == MarketSpec("btc", "5m", 1700000600)

    def test_seconds_until_resolution(self):
        # resolution_ts is the window START; resolution = start + interval_seconds
        # Simulate a window that started 60 seconds ago → resolves in 240 seconds
        start = int(time.time()) - 60
        spec = MarketSpec("btc", "5m", start)
        secs = spec.seconds_until_resolution
        assert 200 < secs <= 240


class TestMarketClock:
    def test_parse_valid(self):
        spec = MarketClock.parse("btc-updown-5m-1700000000")
        assert spec.asset == "btc"
        assert spec.interval_slug == "5m"
        assert spec.resolution_ts == 1700000000

    def test_parse_invalid(self):
        with pytest.raises(ValueError):
            MarketClock.parse("not-a-valid-id")

    def test_current_returns_future_ts(self):
        spec = MarketClock.current("eth", "15m")
        assert spec.asset == "eth"
        assert spec.interval_slug == "15m"
        # resolution_ts is the current window START (≤ now); resolves at start + interval
        assert spec.resolution_ts <= time.time()
        assert spec.seconds_until_resolution > 0

    def test_current_invalid_interval(self):
        with pytest.raises(ValueError):
            MarketClock.current("btc", "99x")

    def test_interval_seconds_dict(self):
        assert INTERVAL_SECONDS["5m"] == 300
        assert INTERVAL_SECONDS["1d"] == 86400


# ---------------------------------------------------------------------------
# models.py
# ---------------------------------------------------------------------------


class TestTrade:
    def make_trade(self, direction="YES", entry=0.6, shares=10) -> Trade:
        return Trade(
            id=str(uuid.uuid4()),
            market_id="btc-updown-5m-9999999999",
            direction=direction,
            shares=shares,
            entry_price=entry,
            entry_time="2026-01-01T00:00:00+00:00",
        )

    def test_is_open(self):
        t = self.make_trade()
        assert t.is_open
        t.exit_price = 0.7
        assert not t.is_open

    def test_unrealised_yes(self):
        t = self.make_trade("YES", entry=0.5, shares=10)
        assert pytest.approx(t.unrealised(0.7), 0.001) == 2.0

    def test_unrealised_no(self):
        t = self.make_trade("NO", entry=0.5, shares=10)
        assert pytest.approx(t.unrealised(0.3), 0.001) == 2.0

    def test_serialise_roundtrip(self):
        t = self.make_trade()
        assert trade_from_dict(trade_to_dict(t)).id == t.id


class TestPortfolio:
    def test_realised_pnl(self):
        p = Portfolio(cash=900.0)
        p.trades = [
            Trade(
                id="1",
                market_id="x",
                direction="YES",
                shares=10,
                entry_price=0.5,
                entry_time="t",
                exit_price=0.6,
                exit_time="t2",
                pnl=1.0,
            )
        ]
        assert p.realised_pnl == 1.0

    def test_win_rate(self):
        p = Portfolio()
        p.trades = [
            Trade("1", "x", "YES", 10, 0.5, "t", exit_price=0.6, exit_time="t2", pnl=1.0),
            Trade("2", "x", "YES", 10, 0.6, "t", exit_price=0.5, exit_time="t2", pnl=-1.0),
        ]
        assert p.win_rate == 0.5

    def test_win_rate_none_when_no_closed(self):
        assert Portfolio().win_rate is None

    def test_portfolio_serialise_roundtrip(self):
        p = Portfolio(cash=500.0)
        p2 = portfolio_from_dict(portfolio_to_dict(p))
        assert p2.cash == 500.0


# ---------------------------------------------------------------------------
# PaperTrader — buy / close / close_all
# ---------------------------------------------------------------------------


class TestPaperTraderBuy:
    def test_buy_yes_deducts_cash(self):
        trader = fresh_trader(cash=1000.0)
        trader._latest_price = make_tick(yes=0.6)
        trade = trader.buy("YES", shares=10)
        assert trade.direction == "YES"
        fee = CRYPTO_FEES.taker_fee(10, 0.6)
        assert pytest.approx(trader.portfolio.cash) == 1000.0 - 10 * 0.6 - fee

    def test_buy_no_deducts_cash(self):
        trader = fresh_trader(cash=1000.0)
        trader._latest_price = make_tick(no=0.4)
        trade = trader.buy("NO", shares=5)
        fee = CRYPTO_FEES.taker_fee(5, 0.4)
        assert pytest.approx(trader.portfolio.cash) == 1000.0 - 5 * 0.4 - fee

    def test_buy_with_explicit_price(self):
        trader = fresh_trader(cash=1000.0)
        trade = trader.buy("YES", shares=10, price=0.5)
        assert trade.entry_price == 0.5

    def test_buy_no_price_raises(self):
        trader = fresh_trader()
        with pytest.raises(NoPriceAvailableError):
            trader.buy("YES", shares=1)

    def test_buy_insufficient_funds(self):
        trader = fresh_trader(cash=1.0)
        trader._latest_price = make_tick(yes=0.6)
        with pytest.raises(InsufficientFundsError):
            trader.buy("YES", shares=100)

    def test_buy_below_minimum_order(self):
        trader = fresh_trader()
        with pytest.raises(MinimumOrderError):
            trader.buy("YES", shares=1, price=0.50)   # cost = $0.50 < $1.00

    def test_buy_exactly_minimum_order(self):
        trader = fresh_trader()
        trade = trader.buy("YES", shares=2, price=0.50)   # cost = $1.00 — OK
        assert trade.is_open


class TestPaperTraderClose:
    def _open_trade(self, trader, direction="YES", price=0.5, shares=10) -> Trade:
        return trader.buy(direction, shares=shares, price=price)

    def test_close_yes_positive_pnl(self):
        trader = fresh_trader()
        trade = self._open_trade(trader, "YES", price=0.5)
        closed = trader.close(trade.id, price=0.7)
        entry_fee = CRYPTO_FEES.taker_fee(10, 0.5)
        exit_fee  = CRYPTO_FEES.taker_fee(10, 0.7)
        assert pytest.approx(closed.pnl) == (0.7 - 0.5) * 10 - entry_fee - exit_fee

    def test_close_no_positive_pnl(self):
        trader = fresh_trader()
        trade = self._open_trade(trader, "NO", price=0.5)
        closed = trader.close(trade.id, price=0.3)
        entry_fee = CRYPTO_FEES.taker_fee(10, 0.5)
        exit_fee  = CRYPTO_FEES.taker_fee(10, 0.3)
        assert pytest.approx(closed.pnl) == (0.5 - 0.3) * 10 - entry_fee - exit_fee

    def test_close_credits_cash(self):
        trader = fresh_trader(cash=1000.0)
        trade = self._open_trade(trader, "YES", price=0.5, shares=10)
        cash_after_buy = trader.portfolio.cash
        trader.close(trade.id, price=0.7)
        exit_fee = CRYPTO_FEES.taker_fee(10, 0.7)
        assert pytest.approx(trader.portfolio.cash) == cash_after_buy + 10 * 0.7 - exit_fee

    def test_close_not_found(self):
        trader = fresh_trader()
        with pytest.raises(TradeNotFoundError):
            trader.close("nonexistent-id")

    def test_close_already_closed(self):
        trader = fresh_trader()
        trade = self._open_trade(trader)
        trader.close(trade.id, price=0.6)
        with pytest.raises(TradeAlreadyClosedError):
            trader.close(trade.id, price=0.7)

    def test_close_no_price_raises(self):
        trader = fresh_trader()
        trade = self._open_trade(trader, price=0.5)
        with pytest.raises(NoPriceAvailableError):
            trader.close(trade.id)

    def test_close_all(self):
        trader = fresh_trader()
        trader.buy("YES", shares=5, price=0.5)
        trader.buy("NO", shares=5, price=0.5)
        closed = trader.close_all(price=0.6)
        assert len(closed) == 2
        assert all(not t.is_open for t in closed)


# ---------------------------------------------------------------------------
# PaperTrader — summary
# ---------------------------------------------------------------------------


class TestSummary:
    def test_summary_keys(self):
        trader = fresh_trader()
        s = trader.summary()
        for key in (
            "cash",
            "open_trades",
            "closed_trades",
            "realised_pnl",
            "unrealised_pnl",
            "total_pnl",
            "win_rate",
            "market_id",
            "latest_yes_price",
            "latest_no_price",
            "last_rotation",
        ):
            assert key in s, f"Missing key: {key}"

    def test_summary_with_price(self):
        trader = fresh_trader()
        trader._latest_price = make_tick(yes=0.65, no=0.35)
        s = trader.summary()
        assert s["latest_yes_price"] == 0.65
        assert s["latest_no_price"] == 0.35


# ---------------------------------------------------------------------------
# Force-close on rotation
# ---------------------------------------------------------------------------


class TestForceClose:
    def test_force_close_sets_flag(self):
        from polymarket_trader.models import MarketRotationTick

        trader = fresh_trader()
        trader.buy("YES", shares=5, price=0.5)
        trader._latest_price = make_tick(yes=0.6)

        tick = MarketRotationTick(
            old_market_id=trader.market_id,
            new_market_id="btc-updown-5m-9999999999",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        trader._force_close_all(tick)

        closed = trader.portfolio.closed_trades
        assert len(closed) == 1
        assert closed[0].force_closed is True

    def test_force_close_updates_last_rotation(self):
        from polymarket_trader.models import MarketRotationTick

        trader = fresh_trader()
        trader.buy("YES", shares=5, price=0.5)

        tick = MarketRotationTick(
            old_market_id=trader.market_id,
            new_market_id="btc-updown-5m-9999999999",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        trader._force_close_all(tick)
        assert trader._last_rotation is not None
        assert "force_closed_trades" in trader._last_rotation


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------


class TestFeeModel:
    def test_no_fees_zero(self):
        assert NO_FEES.taker_fee(100, 0.5) == 0.0
        assert NO_FEES.maker_fee(100, 0.5) == 0.0

    def test_crypto_taker_fee_at_midpoint(self):
        # fee = shares * price * 0.25 * (price * (1 - price))^2
        # = 100 * 0.5 * 0.25 * (0.5 * 0.5)^2 = 100 * 0.5 * 0.25 * 0.0625 = 0.78125
        fee = CRYPTO_FEES.taker_fee(100, 0.5)
        assert pytest.approx(fee, rel=1e-5) == 0.78125

    def test_crypto_fee_near_zero_at_extremes(self):
        # price very close to 0 or 1 → (price*(1-price))^2 is tiny → rounds to 0
        assert CRYPTO_FEES.taker_fee(1, 0.001) == 0.0
        assert CRYPTO_FEES.taker_fee(1, 0.999) == 0.0

    def test_crypto_maker_fee_less_than_taker(self):
        taker = CRYPTO_FEES.taker_fee(100, 0.5)
        maker = CRYPTO_FEES.maker_fee(100, 0.5)
        assert maker < taker
        assert pytest.approx(maker, rel=1e-5) == taker * (1 - CRYPTO_FEES.maker_rebate)

    def test_sports_fee_less_than_crypto(self):
        crypto = CRYPTO_FEES.taker_fee(100, 0.5)
        sports = SPORTS_FEES.taker_fee(100, 0.5)
        assert sports < crypto

    def test_effective_rate_zero_at_extremes(self):
        assert CRYPTO_FEES.effective_rate(0.0) == 0.0
        assert CRYPTO_FEES.effective_rate(1.0) == 0.0

    def test_detect_fee_model_crypto(self):
        assert detect_fee_model("btc") is CRYPTO_FEES
        assert detect_fee_model("ETH") is CRYPTO_FEES

    def test_detect_fee_model_default_no_fees(self):
        assert detect_fee_model("trumpwin") is NO_FEES

    def test_buy_with_fees_deducts_fee_from_cash(self):
        trader = fresh_trader(cash=1000.0, fee_model=CRYPTO_FEES)
        trade = trader.buy("YES", shares=100, price=0.5)
        expected_fee = CRYPTO_FEES.taker_fee(100, 0.5)
        assert trade.entry_fee == expected_fee
        assert pytest.approx(trader.portfolio.cash) == 1000.0 - 100 * 0.5 - expected_fee

    def test_close_pnl_includes_fees(self):
        trader = fresh_trader(cash=1000.0, fee_model=CRYPTO_FEES)
        trade = trader.buy("YES", shares=100, price=0.5)
        entry_fee = trade.entry_fee
        closed = trader.close(trade.id, price=0.6)
        exit_fee = CRYPTO_FEES.taker_fee(100, 0.6)
        expected_pnl = (0.6 - 0.5) * 100 - entry_fee - exit_fee
        assert pytest.approx(closed.pnl, rel=1e-5) == expected_pnl
        assert closed.exit_fee == exit_fee

    def test_close_maker_uses_rebate(self):
        trader = fresh_trader(cash=1000.0, fee_model=CRYPTO_FEES)
        trade = trader.buy("YES", shares=100, price=0.5)
        closed = trader.close(trade.id, price=0.6, maker=True)
        expected_exit_fee = CRYPTO_FEES.maker_fee(100, 0.6)
        assert closed.exit_fee == expected_exit_fee

    def test_total_fees_property(self):
        trader = fresh_trader(cash=1000.0, fee_model=CRYPTO_FEES)
        trade = trader.buy("YES", shares=100, price=0.5)
        closed = trader.close(trade.id, price=0.6)
        assert closed.total_fees == closed.entry_fee + closed.exit_fee

    def test_summary_includes_fee_model(self):
        trader = fresh_trader(fee_model=CRYPTO_FEES)
        s = trader.summary()
        assert "fee_model" in s
        assert s["fee_model"]["fee_rate"] == CRYPTO_FEES.fee_rate


# ---------------------------------------------------------------------------
# Order Types
# ---------------------------------------------------------------------------


class TestOrderTypes:
    """Tests for TIF order types: MARKET, FOK, FAK, GTC, GTD, post_only."""

    # --- MARKET (backward compat) ---

    def test_market_default_unchanged(self):
        trader = fresh_trader(cash=1000.0)
        trade = trader.buy("YES", shares=10, price=0.5)
        assert isinstance(trade, Trade)
        assert trade.is_open

    def test_market_explicit_tif(self):
        trader = fresh_trader(cash=1000.0)
        trade = trader.buy("YES", shares=10, price=0.5, tif=TimeInForce.MARKET)
        assert isinstance(trade, Trade)

    def test_market_tif_str_coercion(self):
        trader = fresh_trader(cash=1000.0)
        trade = trader.buy("YES", shares=10, price=0.5, tif="MARKET")
        assert isinstance(trade, Trade)

    # --- FOK ---

    def test_fok_fills_when_sufficient_depth(self):
        trader = fresh_trader(cash=1000.0)
        # Provide enough YES asks below limit
        tick = make_tick(
            yes=0.6,
            yes_asks=[Level(price=0.55, size=20)],
        )
        trader._latest_price = tick
        trade = trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.FOK)
        assert isinstance(trade, Trade)
        assert trade.shares == 10

    def test_fok_raises_when_insufficient_depth(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(
            yes=0.6,
            yes_asks=[Level(price=0.55, size=5)],  # only 5, need 10
        )
        trader._latest_price = tick
        with pytest.raises(InsufficientLiquidityError):
            trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.FOK)

    def test_fok_raises_when_limit_price_too_low(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(
            yes=0.6,
            yes_asks=[Level(price=0.70, size=20)],  # ask above limit
        )
        trader._latest_price = tick
        with pytest.raises(InsufficientLiquidityError):
            trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.FOK)

    def test_fok_no_price_uses_all_levels(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(
            yes=0.6,
            yes_asks=[Level(price=0.59, size=10)],
        )
        trader._latest_price = tick
        trade = trader.buy("YES", shares=10, tif=TimeInForce.FOK)
        assert isinstance(trade, Trade)

    # --- FAK ---

    def test_fak_partial_fill(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(
            yes=0.6,
            yes_asks=[Level(price=0.55, size=4)],  # only 4 available
        )
        trader._latest_price = tick
        trade = trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.FAK)
        assert isinstance(trade, Trade)
        assert trade.shares == 4  # actual fill

    def test_fak_full_fill_when_enough_depth(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(
            yes=0.6,
            yes_asks=[Level(price=0.55, size=20)],
        )
        trader._latest_price = tick
        trade = trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.FAK)
        assert trade.shares == 10

    def test_fak_raises_when_no_liquidity(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(
            yes=0.6,
            yes_asks=[Level(price=0.70, size=20)],  # all above limit
        )
        trader._latest_price = tick
        with pytest.raises(InsufficientLiquidityError):
            trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.FAK)

    # --- GTC ---

    def test_gtc_requires_price(self):
        trader = fresh_trader(cash=1000.0)
        with pytest.raises(ValueError):
            trader.buy("YES", shares=10, tif=TimeInForce.GTC)

    def test_gtc_immediate_fill_when_crosses(self):
        trader = fresh_trader(cash=1000.0)
        # best_ask (0.55) <= limit_price (0.60) → immediate taker fill
        tick = make_tick(
            yes=0.6,
            yes_asks=[Level(price=0.55, size=20)],
        )
        trader._latest_price = tick
        result = trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.GTC)
        assert isinstance(result, Trade)
        assert result.is_open

    def test_gtc_stored_when_no_cross(self):
        trader = fresh_trader(cash=1000.0)
        # best_ask (0.70) > limit_price (0.60) → resting order
        tick = make_tick(
            yes=0.7,
            yes_asks=[Level(price=0.70, size=20)],
        )
        trader._latest_price = tick
        result = trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.GTC)
        assert isinstance(result, PendingOrder)
        assert result.tif == TimeInForce.GTC
        assert len(trader.portfolio.pending_orders) == 1

    def test_gtc_reserves_cash(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(
            yes=0.7,
            yes_asks=[Level(price=0.70, size=20)],
        )
        trader._latest_price = tick
        trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.GTC)
        # cash should be reduced by reserved amount
        assert pytest.approx(trader.portfolio.cash) == 1000.0 - 10 * 0.60

    def test_gtc_no_price_raises(self):
        trader = fresh_trader(cash=1000.0)
        trader._latest_price = None
        with pytest.raises(ValueError):
            trader.buy("YES", shares=10, tif=TimeInForce.GTC)

    # --- GTC post_only ---

    def test_gtc_post_only_raises_when_crosses(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(
            yes=0.6,
            yes_asks=[Level(price=0.55, size=20)],
        )
        trader._latest_price = tick
        with pytest.raises(PostOnlyCancelledError):
            trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.GTC, post_only=True)

    def test_gtc_post_only_stores_when_no_cross(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(
            yes=0.7,
            yes_asks=[Level(price=0.70, size=20)],
        )
        trader._latest_price = tick
        result = trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.GTC, post_only=True)
        assert isinstance(result, PendingOrder)
        assert result.post_only is True

    # --- GTD expiry ---

    def test_gtd_expires_on_check(self):
        trader = fresh_trader(cash=1000.0)
        # Create a GTD order with expiry in the past
        tick = make_tick(
            yes=0.7,
            yes_asks=[Level(price=0.70, size=20)],
        )
        trader._latest_price = tick
        past_expiry = time.time() - 1.0  # already expired
        result = trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.GTD, expiration=past_expiry)
        assert isinstance(result, PendingOrder)
        assert trader.portfolio.cash == pytest.approx(1000.0 - 10 * 0.60)

        # Now trigger _check_pending_orders — should expire and release cash
        fill_events = trader._check_pending_orders(tick)
        assert len(fill_events) == 0
        assert len(trader.portfolio.pending_orders) == 0
        assert trader.portfolio.cash == pytest.approx(1000.0 - 10 * 0.60 + 10 * 0.60)  # cash restored

    def test_gtd_fills_before_expiry(self):
        trader = fresh_trader(cash=1000.0)
        future_expiry = time.time() + 3600
        # First place as resting (ask > limit)
        setup_tick = make_tick(yes=0.7, yes_asks=[Level(price=0.70, size=20)])
        trader._latest_price = setup_tick
        result = trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.GTD, expiration=future_expiry)
        assert isinstance(result, PendingOrder)

        # Tick with ask crossing limit → should fill
        fill_tick = make_tick(yes=0.55, yes_asks=[Level(price=0.55, size=20)])
        fill_events = trader._check_pending_orders(fill_tick)
        assert len(fill_events) == 1
        assert isinstance(fill_events[0].trade, Trade)

    # --- cancel_order ---

    def test_cancel_order_releases_cash(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(yes=0.7, yes_asks=[Level(price=0.70, size=20)])
        trader._latest_price = tick
        order = trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.GTC)
        assert isinstance(order, PendingOrder)
        reserved = 10 * 0.60  # $6.00
        assert trader.portfolio.cash == pytest.approx(1000.0 - reserved)

        cancelled = trader.cancel_order(order.id)
        assert cancelled.id == order.id
        assert len(trader.portfolio.pending_orders) == 0
        assert trader.portfolio.cash == pytest.approx(1000.0)

    def test_cancel_order_not_found(self):
        trader = fresh_trader(cash=1000.0)
        with pytest.raises(OrderNotFoundError):
            trader.cancel_order("nonexistent-id")

    # --- Close with GTC ---

    def test_close_gtc_stored_when_no_cross(self):
        trader = fresh_trader(cash=1000.0)
        trade = trader.buy("YES", shares=10, price=0.50)

        # best_bid (0.40) < limit_price (0.60) → resting close order
        tick = make_tick(yes=0.5, yes_bids=[Level(price=0.40, size=20)])
        trader._latest_price = tick
        result = trader.close(trade.id, price=0.60, tif=TimeInForce.GTC)
        assert isinstance(result, PendingOrder)
        assert result.close_trade_id == trade.id

    def test_close_gtc_immediate_fill_when_crosses(self):
        trader = fresh_trader(cash=1000.0)
        trade = trader.buy("YES", shares=10, price=0.50)

        # best_bid (0.65) >= limit_price (0.60) → immediate fill
        tick = make_tick(yes=0.65, yes_bids=[Level(price=0.65, size=20)])
        trader._latest_price = tick
        result = trader.close(trade.id, price=0.60, tif=TimeInForce.GTC)
        assert isinstance(result, Trade)
        assert not result.is_open

    def test_close_gtc_fills_on_tick(self):
        trader = fresh_trader(cash=1000.0)
        trade = trader.buy("YES", shares=10, price=0.50)
        cash_after_buy = trader.portfolio.cash

        # Place resting close order
        setup_tick = make_tick(yes=0.5, yes_bids=[Level(price=0.40, size=20)])
        trader._latest_price = setup_tick
        trader.close(trade.id, price=0.60, tif=TimeInForce.GTC)

        # Tick with bid crossing limit → should fill
        fill_tick = make_tick(yes=0.65, yes_bids=[Level(price=0.65, size=20)])
        fill_events = trader._check_pending_orders(fill_tick)
        assert len(fill_events) == 1
        assert not fill_events[0].trade.is_open
        assert len(trader.portfolio.pending_orders) == 0

    # --- PendingOrder serialisation ---

    def test_pending_order_roundtrip(self):
        order = PendingOrder(
            id=str(uuid.uuid4()),
            market_id="btc-updown-5m-9999999999",
            direction="YES",
            shares=10,
            limit_price=0.60,
            tif=TimeInForce.GTD,
            post_only=False,
            created_at="2026-01-01T00:00:00+00:00",
            expiration=1700000000.0,
            close_trade_id=None,
        )
        restored = order_from_dict(order_to_dict(order))
        assert restored.id == order.id
        assert restored.tif == TimeInForce.GTD
        assert restored.expiration == 1700000000.0

    def test_portfolio_serialise_with_pending_orders(self):
        p = Portfolio(cash=900.0)
        p.pending_orders = [
            PendingOrder(
                id="ord-1",
                market_id="btc-updown-5m-9999999999",
                direction="YES",
                shares=10,
                limit_price=0.60,
                tif=TimeInForce.GTC,
                post_only=False,
                created_at="2026-01-01T00:00:00+00:00",
                expiration=None,
                close_trade_id=None,
            )
        ]
        p2 = portfolio_from_dict(portfolio_to_dict(p))
        assert len(p2.pending_orders) == 1
        assert p2.pending_orders[0].tif == TimeInForce.GTC

    def test_old_json_no_pending_orders_loads_cleanly(self):
        """Backward compat: state files without pending_orders key load fine."""
        old_dict = {
            "cash": 500.0,
            "trades": [],
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            # no 'pending_orders' key
        }
        p = portfolio_from_dict(old_dict)
        assert p.pending_orders == []
        assert p.cash == 500.0

    # --- summary includes pending_orders and reserved_cash ---

    def test_summary_includes_pending_and_reserved(self):
        trader = fresh_trader(cash=1000.0)
        tick = make_tick(yes=0.7, yes_asks=[Level(price=0.70, size=20)])
        trader._latest_price = tick
        trader.buy("YES", shares=10, price=0.60, tif=TimeInForce.GTC)
        s = trader.summary()
        assert s["pending_orders"] == 1
        assert s["reserved_cash"] == pytest.approx(10 * 0.60)
