"""Unit tests for polymarket_trader — no network calls."""

from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from polymarket_trader import (
    InsufficientFundsError,
    NoPriceAvailableError,
    PaperTrader,
    TradeAlreadyClosedError,
    TradeNotFoundError,
)
from polymarket_trader.models import (
    OrderBook,
    Portfolio,
    PriceTick,
    Trade,
    portfolio_from_dict,
    portfolio_to_dict,
    trade_from_dict,
    trade_to_dict,
)
from polymarket_trader.utils import INTERVAL_SECONDS, MarketClock, MarketSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_tick(yes=0.6, no=0.4, market_id="btc-updown-5m-9999999999") -> PriceTick:
    return PriceTick(
        market_id=market_id,
        yes_price=yes,
        no_price=no,
        timestamp="2026-01-01T00:00:00+00:00",
        order_book=OrderBook([], [], [], []),
    )


def fresh_trader(**kwargs) -> PaperTrader:
    """PaperTrader backed by a fresh in-memory portfolio (no disk I/O)."""
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
        assert pytest.approx(trader.portfolio.cash) == 1000.0 - 10 * 0.6

    def test_buy_no_deducts_cash(self):
        trader = fresh_trader(cash=1000.0)
        trader._latest_price = make_tick(no=0.4)
        trade = trader.buy("NO", shares=5)
        assert pytest.approx(trader.portfolio.cash) == 1000.0 - 5 * 0.4

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


class TestPaperTraderClose:
    def _open_trade(self, trader, direction="YES", price=0.5, shares=10) -> Trade:
        return trader.buy(direction, shares=shares, price=price)

    def test_close_yes_positive_pnl(self):
        trader = fresh_trader()
        trade = self._open_trade(trader, "YES", price=0.5)
        closed = trader.close(trade.id, price=0.7)
        assert pytest.approx(closed.pnl) == (0.7 - 0.5) * 10

    def test_close_no_positive_pnl(self):
        trader = fresh_trader()
        trade = self._open_trade(trader, "NO", price=0.5)
        closed = trader.close(trade.id, price=0.3)
        assert pytest.approx(closed.pnl) == (0.5 - 0.3) * 10

    def test_close_credits_cash(self):
        trader = fresh_trader(cash=1000.0)
        trade = self._open_trade(trader, "YES", price=0.5, shares=10)
        cash_after_buy = trader.portfolio.cash
        trader.close(trade.id, price=0.7)
        assert pytest.approx(trader.portfolio.cash) == cash_after_buy + 10 * 0.7

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
