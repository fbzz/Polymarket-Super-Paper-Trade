"""
BTC 5-minute — VERBOSE mode.

Shows everything: every tick with full microstructure data, order book
every 15 ticks, immediate feedback on every trade action and bankroll change.
"""

import asyncio
import logging

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

trader     = PaperTrader(asset="btc", interval="5m", initial_cash=500.0)
stats      = TickStats(window=20)
tick_count = 0
open_trade = None


def _bankroll_line() -> str:
    p   = trader.portfolio
    lp  = trader.latest_price
    unreal = 0.0
    if lp and p.open_trades:
        for t in p.open_trades:
            unreal += t.unrealised(lp.yes_price if t.direction == "YES" else lp.no_price)
    return (
        f"  💰 Cash {fmt_cash(p.cash)}"
        f"  │  Realised {fmt_pnl(p.realised_pnl)}"
        f"  │  Unrealised {fmt_pnl(unreal)}"
        f"  │  Open {len(p.open_trades)}  Closed {len(p.closed_trades)}"
    )


async def on_tick(event):
    global tick_count, open_trade

    # ── rotation ──────────────────────────────────────────────────────────
    if isinstance(event, MarketRotationTick):
        print_rotation(event)
        print_summary(trader.summary())
        tick_count = 0
        open_trade = None
        return

    tick: PriceTick = event
    stats.update(tick)
    tick_count += 1

    # ── every tick: full microstructure line ───────────────────────────────
    print_tick_rich(tick, tick_count, stats)

    # ── every 15 ticks: order book panel ──────────────────────────────────
    if tick_count % 15 == 0:
        print_orderbook(tick.order_book, tick.market_id)
        print(_bankroll_line())
        print()

    # ── strategy: buy YES on tick 3, close on tick 15 ─────────────────────
    if tick_count == 3:
        open_trade = trader.buy("YES", shares=10)
        print()
        print_trade_opened(open_trade)
        print(_bankroll_line())
        print()

    elif tick_count == 15 and open_trade:
        for t in trader.close_all():
            print()
            print_trade_closed(t)
        open_trade = None
        print(_bankroll_line())
        print_summary(trader.summary())


async def main():
    print_startup(trader.market_id, trader.portfolio.cash)
    await trader.stream(on_tick)


if __name__ == "__main__":
    asyncio.run(main())
