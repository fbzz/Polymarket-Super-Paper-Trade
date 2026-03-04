"""
BTC 5-minute — NORMAL mode.

Quiet stream. Trades every ~60 seconds (close + reopen).
Output focuses on:
  • Order placed / settled + bankroll change
  • 60-second market digest
  • Market rotation summary
"""

import asyncio
import logging
import time

from polymarket_trader import (
    InsufficientFundsError,
    MinimumOrderError,
    PaperTrader,
    TickStats,
    fmt_cash,
    fmt_pnl,
    fmt_price,
    print_rotation,
    print_startup,
    print_summary,
    print_trade_closed,
    print_trade_opened,
)
from polymarket_trader.models import MarketRotationTick, PriceTick

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("websockets").setLevel(logging.ERROR)

trader       = PaperTrader(asset="btc", interval="5m", initial_cash=500.0)
stats        = TickStats(window=20)
open_trade   = None
_last_trade  = 0.0    # wall-clock time of last open
_last_digest = 0.0    # wall-clock time of last digest line
_TRADE_EVERY  = 60    # open a new position every 60 s
_DIGEST_EVERY = 60    # status digest every 60 s
_trade_num    = 0     # how many trades placed this window


def _bankroll(label: str) -> None:
    p      = trader.portfolio
    lp     = trader.latest_price
    unreal = 0.0
    if lp and p.open_trades:
        for t in p.open_trades:
            price = lp.yes_price if t.direction == "YES" else lp.no_price
            unreal += t.unrealised(price)
    print(
        f"  {label:<22}"
        f"  Cash {fmt_cash(p.cash)}"
        f"  │  Realised {fmt_pnl(p.realised_pnl)}"
        f"  Unrealised {fmt_pnl(unreal)}"
        f"  │  Open {len(p.open_trades)}  Closed {len(p.closed_trades)}"
    )


def _digest(tick: PriceTick) -> None:
    vol   = stats.volatility
    mom   = stats.momentum
    vol_s = f"vol {vol:.4f}" if vol is not None else "vol n/a "
    mom_s = f"mom {mom:+.4f}" if mom is not None else "mom n/a "
    ts    = tick.timestamp[11:19]
    print(
        f"  ── {ts}"
        f"  YES {fmt_price(tick.yes_price)}  NO {fmt_price(tick.no_price)}"
        f"  {vol_s}  {mom_s}"
    )


def _pick_direction() -> str:
    """Simple signal: follow momentum if strong enough, else YES."""
    mom = stats.momentum
    if mom is not None and mom < -0.01:
        return "NO"
    return "YES"


async def on_tick(event):
    global open_trade, _last_trade, _last_digest, _trade_num

    # ── rotation ──────────────────────────────────────────────────────────
    if isinstance(event, MarketRotationTick):
        print_rotation(event)
        print_summary(trader.summary())
        open_trade   = None
        _last_trade  = 0.0
        _trade_num   = 0
        _last_digest = time.time()
        return

    tick: PriceTick = event
    stats.update(tick)
    now = time.time()

    # ── 60-second digest ──────────────────────────────────────────────────
    if now - _last_digest >= _DIGEST_EVERY:
        _digest(tick)
        _last_digest = now

    # ── trade every 60 seconds ────────────────────────────────────────────
    if now - _last_trade >= _TRADE_EVERY:
        # close any open position first
        if open_trade:
            closed = trader.close_all()
            print()
            for t in closed:
                print_trade_closed(t)
            _bankroll("Bankroll after close:")
            print()
            open_trade = None

        # open a new position
        direction = _pick_direction()
        try:
            open_trade = trader.buy(direction, shares=10)
        except (MinimumOrderError, InsufficientFundsError):
            _last_trade = now
            return
        _trade_num += 1
        _last_trade = now
        print()
        print_trade_opened(open_trade)
        _bankroll(f"Bankroll after buy #{_trade_num}:")
        print()


async def main():
    global _last_digest, _last_trade
    _last_digest = time.time()
    _last_trade  = time.time() - _TRADE_EVERY   # fire first trade immediately
    print_startup(trader.market_id, trader.portfolio.cash)
    await trader.stream(on_tick)


if __name__ == "__main__":
    asyncio.run(main())
