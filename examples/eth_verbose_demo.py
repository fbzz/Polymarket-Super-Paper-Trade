"""
ETH 15-minute — VERBOSE mode.

Shows everything: every tick with full microstructure, order book every 15
ticks, immediate bankroll feedback on every action, full summary on rotation.
Trades every 3 minutes so you see multiple cycles per window.
"""

import asyncio
import logging
import time

from polymarket_trader import (
    PaperTrader,
    TickStats,
    fmt_cash,
    fmt_pnl,
    print_orderbook,
    print_rotation,
    print_startup,
    print_summary,
    print_tick_rich,
    print_trade_closed,
    print_trade_opened,
)
from polymarket_trader.models import MarketRotationTick, PriceTick

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("websockets").setLevel(logging.ERROR)

trader       = PaperTrader(asset="eth", interval="15m", initial_cash=500.0, state_file="eth_state.json")
stats        = TickStats(window=30)
tick_count   = 0
open_trade   = None
_last_trade  = 0.0
_TRADE_EVERY = 180   # new position every 3 minutes
_trade_num   = 0


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


def _pick_direction() -> str:
    mom = stats.momentum
    if mom is not None and mom < -0.01:
        return "NO"
    return "YES"


async def on_tick(event):
    global tick_count, open_trade, _last_trade, _trade_num

    # ── rotation ──────────────────────────────────────────────────────────
    if isinstance(event, MarketRotationTick):
        print_rotation(event)
        print_summary(trader.summary())
        tick_count  = 0
        open_trade  = None
        _last_trade = 0.0
        _trade_num  = 0
        return

    tick: PriceTick = event
    stats.update(tick)
    tick_count += 1
    now = time.time()

    # ── every tick: full microstructure ───────────────────────────────────
    print_tick_rich(tick, tick_count, stats)

    # ── order book every 15 ticks ─────────────────────────────────────────
    if tick_count % 15 == 0:
        print_orderbook(tick.order_book, tick.market_id)
        _bankroll("Current bankroll:")
        print()

    # ── trade every 3 minutes ─────────────────────────────────────────────
    if now - _last_trade >= _TRADE_EVERY:
        # close existing
        if open_trade:
            closed = trader.close_all()
            print()
            for t in closed:
                print_trade_closed(t)
            _bankroll("Bankroll after close:")
            print()
            open_trade = None

        # open new
        direction  = _pick_direction()
        open_trade = trader.buy(direction, shares=10)
        _trade_num += 1
        _last_trade = now
        print()
        print_trade_opened(open_trade)
        _bankroll(f"Bankroll after buy #{_trade_num}:")
        print()


async def main():
    global _last_trade
    _last_trade = time.time() - _TRADE_EVERY   # fire first trade immediately
    print_startup(trader.market_id, trader.portfolio.cash)
    await trader.stream(on_tick)


if __name__ == "__main__":
    asyncio.run(main())
