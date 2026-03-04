"""
BTC 5-minute — NORMAL mode.

Quiet stream. Output focuses on:
  • When an order is placed
  • When a position is closed / settled
  • When the bankroll changes
  • A market status digest every 60 seconds
  • Market rotation events
"""

import asyncio
import logging
import time

from polymarket_trader import (
    PaperTrader,
    TickStats,
    fmt_cash,
    fmt_price,
    fmt_pnl,
    print_rotation,
    print_startup,
    print_summary,
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
_last_digest = time.time()
_DIGEST_EVERY = 60   # seconds between status digests


def _print_digest(tick: PriceTick) -> None:
    """One-liner market snapshot — printed periodically, not every tick."""
    p      = trader.portfolio
    lp     = trader.latest_price
    unreal = 0.0
    if lp and p.open_trades:
        for t in p.open_trades:
            unreal += t.unrealised(lp.yes_price if t.direction == "YES" else lp.no_price)

    ts    = tick.timestamp[11:19]
    vol   = stats.volatility
    mom   = stats.momentum
    vol_s = f"vol {vol:.4f}" if vol is not None else "vol n/a"
    mom_s = f"mom {mom:+.4f}" if mom is not None else "mom n/a"

    print(
        f"  ── {ts}  {tick.market_id.split('-')[0].upper()}"
        f"  YES {fmt_price(tick.yes_price)}  NO {fmt_price(tick.no_price)}"
        f"  {vol_s}  {mom_s}"
        f"  │  Cash {fmt_cash(p.cash)}"
        f"  Realised {fmt_pnl(p.realised_pnl)}"
        f"  Unrealised {fmt_pnl(unreal)}"
        f"  Open {len(p.open_trades)}"
    )


async def on_tick(event):
    global tick_count, open_trade, _last_digest

    # ── rotation ──────────────────────────────────────────────────────────
    if isinstance(event, MarketRotationTick):
        print_rotation(event)
        print_summary(trader.summary())
        tick_count  = 0
        open_trade  = None
        _last_digest = time.time()
        return

    tick: PriceTick = event
    stats.update(tick)
    tick_count += 1

    # ── periodic digest (every 60 s) ──────────────────────────────────────
    now = time.time()
    if now - _last_digest >= _DIGEST_EVERY:
        _print_digest(tick)
        _last_digest = now

    # ── strategy: buy YES on tick 5, close on tick 25 ─────────────────────
    if tick_count == 5:
        open_trade = trader.buy("YES", shares=10)
        print()
        print_trade_opened(open_trade)
        print(f"  Bankroll after buy:   Cash {fmt_cash(trader.portfolio.cash)}")
        print()

    elif tick_count == 25 and open_trade:
        closed = trader.close_all()
        print()
        for t in closed:
            print_trade_closed(t)
        p = trader.portfolio
        print(f"  Bankroll after close: Cash {fmt_cash(p.cash)}  Realised {fmt_pnl(p.realised_pnl)}")
        print()
        open_trade = None


async def main():
    print_startup(trader.market_id, trader.portfolio.cash)
    await trader.stream(on_tick)


if __name__ == "__main__":
    asyncio.run(main())
