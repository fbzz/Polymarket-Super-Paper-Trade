"""ETH 15-minute paper trading demo — rich display."""

import asyncio
import logging

from polymarket_trader import (
    PaperTrader,
    TickStats,
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

trader     = PaperTrader(asset="eth", interval="15m", initial_cash=500.0)
stats      = TickStats(window=20)
tick_count = 0
open_trade = None


async def on_tick(event):
    global tick_count, open_trade

    # ── rotation ──────────────────────────────────────────────────────────
    if isinstance(event, MarketRotationTick):
        print_rotation(event)
        print_summary(trader.summary())
        tick_count = 0
        return

    tick: PriceTick = event
    stats.update(tick)
    tick_count += 1

    # ── rich tick line (every tick) ────────────────────────────────────────
    print_tick_rich(tick, tick_count, stats)

    # ── order book (every 15 ticks) ────────────────────────────────────────
    if tick_count % 15 == 0:
        print_orderbook(tick.order_book, tick.market_id)

    # ── strategy: buy NO on tick 1, close on tick 10 ──────────────────────
    if tick_count == 1:
        open_trade = trader.buy("NO", shares=10)
        print_trade_opened(open_trade)

    elif tick_count == 10 and open_trade:
        for t in trader.close_all():
            print_trade_closed(t)
        open_trade = None
        print_summary(trader.summary())


async def main():
    print_startup(trader.market_id, trader.portfolio.cash)
    await trader.stream(on_tick)


if __name__ == "__main__":
    asyncio.run(main())
