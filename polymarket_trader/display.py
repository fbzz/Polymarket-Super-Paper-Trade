"""
Console display utilities for polymarket_trader.

Zero extra dependencies — pure ANSI escape codes.
Automatically disables colour when stdout is not a TTY or NO_COLOR is set.
"""

from __future__ import annotations

import math
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import MarketRotationTick, OrderBook, PriceTick, Trade

# ---------------------------------------------------------------------------
# Colour support
# ---------------------------------------------------------------------------

def _colours_enabled() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    return sys.stdout.isatty()


_COLOUR = _colours_enabled()

_R  = "\033[0m"
_B  = "\033[1m"
_DM = "\033[2m"
_GR = "\033[92m"
_RD = "\033[91m"
_YL = "\033[93m"
_CY = "\033[96m"
_WH = "\033[97m"
_MG = "\033[95m"
_BL = "\033[94m"


def _c(code: str, text: str) -> str:
    if not _COLOUR:
        return text
    return f"{code}{text}{_R}"


def _strip_ansi(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", s)


# ---------------------------------------------------------------------------
# Rolling market statistics tracker
# ---------------------------------------------------------------------------

@dataclass
class TickStats:
    """
    Pass one instance through your on_tick callback and call .update(tick)
    each time. Exposes volatility, momentum, and bid/ask imbalance.

    Example::

        stats = TickStats()

        async def on_tick(event):
            if isinstance(event, PriceTick):
                stats.update(event)
                print_tick_rich(event, count, stats)
    """
    window: int = 20
    _prices: deque = field(default_factory=lambda: deque(maxlen=20), init=False, repr=False)

    def __post_init__(self) -> None:
        self._prices = deque(maxlen=self.window)

    def update(self, tick: "PriceTick") -> None:
        self._prices.append(tick.yes_price)

    # -- derived metrics --

    @property
    def prices(self) -> list[float]:
        return list(self._prices)

    @property
    def volatility(self) -> float | None:
        """Rolling std-dev of tick-to-tick price changes."""
        p = self.prices
        if len(p) < 3:
            return None
        returns = [p[i] - p[i - 1] for i in range(1, len(p))]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance)

    @property
    def momentum(self) -> float | None:
        """Price change from N ticks ago (signed)."""
        p = self.prices
        if len(p) < 2:
            return None
        return p[-1] - p[0]

    @property
    def delta(self) -> float | None:
        """Price change vs previous tick."""
        p = self.prices
        if len(p) < 2:
            return None
        return p[-1] - p[-2]

    def imbalance(self, order_book: "OrderBook") -> float | None:
        """
        Bid/ask size imbalance for YES token: +1.0 = all bids, -1.0 = all asks.
        Uses top-3 levels.
        """
        bids = order_book.yes_bids[:3]
        asks = order_book.yes_asks[:3]
        bid_vol = sum(l.size for l in bids)
        ask_vol = sum(l.size for l in asks)
        total = bid_vol + ask_vol
        if total == 0:
            return None
        return (bid_vol - ask_vol) / total


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def fmt_pnl(value: float) -> str:
    sign = "+" if value >= 0 else ""
    s = f"{sign}{value:.4f}"
    return _c(_GR if value > 0 else (_RD if value < 0 else _DM), s)


def fmt_price(price: float) -> str:
    s = f"{price:.4f}"
    if price >= 0.65:
        return _c(_GR, s)
    if price <= 0.35:
        return _c(_RD, s)
    return _c(_YL, s)


def fmt_cash(value: float) -> str:
    return _c(_WH, f"${value:,.2f}")


def fmt_direction(direction: str) -> str:
    return _c(_GR if direction == "YES" else _RD, f"{direction:3s}")


def fmt_id(trade_id: str) -> str:
    return _c(_DM, trade_id[:8] + "…")


def fmt_win_rate(rate: float | None) -> str:
    if rate is None:
        return _c(_DM, "n/a")
    pct = rate * 100
    return _c(_GR if pct >= 60 else (_RD if pct < 40 else _YL), f"{pct:.1f}%")


def fmt_delta(value: float | None) -> str:
    if value is None:
        return _c(_DM, "  ─    ")
    arrow = "▲" if value > 0 else ("▼" if value < 0 else "─")
    color = _GR if value > 0 else (_RD if value < 0 else _DM)
    return _c(color, f"{arrow}{value:+.4f}")


def fmt_vol(value: float | None) -> str:
    if value is None:
        return _c(_DM, "vol  n/a ")
    color = _RD if value > 0.005 else (_YL if value > 0.002 else _GR)
    return _c(_DM, "vol ") + _c(color, f"{value:.4f}")


def fmt_imbalance(value: float | None) -> str:
    if value is None:
        return _c(_DM, "imb  n/a ")
    bar_len = 7
    filled = round(abs(value) * bar_len)
    if value > 0.1:
        bar = _c(_GR, "█" * filled) + _c(_DM, "░" * (bar_len - filled))
        label = _c(_GR, f"+{value:.2f}")
    elif value < -0.1:
        bar = _c(_RD, "█" * filled) + _c(_DM, "░" * (bar_len - filled))
        label = _c(_RD, f"{value:.2f}")
    else:
        bar = _c(_DM, "░" * bar_len)
        label = _c(_DM, f"{value:+.2f}")
    return _c(_DM, "imb ") + label + " " + bar


def fmt_momentum(value: float | None, window: int) -> str:
    if value is None:
        return _c(_DM, "mom  n/a")
    color = _GR if value > 0.001 else (_RD if value < -0.001 else _DM)
    label = f"{value:+.4f}"
    return _c(_DM, f"mom({window}) ") + _c(color, label)


# ---------------------------------------------------------------------------
# Sparkline
# ---------------------------------------------------------------------------

_SPARKS = "▁▂▃▄▅▆▇█"


def fmt_sparkline(prices: list[float], width: int = 20) -> str:
    """Render a mini bar-chart of prices using Unicode block characters."""
    if len(prices) < 2:
        return _c(_DM, "─" * width)
    lo, hi = min(prices), max(prices)
    span = hi - lo or 1e-9
    chars = []
    for p in prices[-width:]:
        idx = int((p - lo) / span * (len(_SPARKS) - 1))
        chars.append(_SPARKS[idx])
    # colour the last bar by direction
    last_delta = prices[-1] - prices[-2] if len(prices) >= 2 else 0
    color = _GR if last_delta > 0 else (_RD if last_delta < 0 else _DM)
    body = "".join(chars[:-1])
    tail = _c(color, chars[-1])
    return _c(_DM, body) + tail


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------

def print_startup(market_id: str, cash: float) -> None:
    print(_c(_B + _CY, "\n  polymarket-trader"))
    print(_c(_DM, "  " + "─" * 70))
    print(f"  Market  {_c(_WH, market_id)}")
    print(f"  Cash    {fmt_cash(cash)}")
    print(_c(_DM, "  " + "─" * 70 + "\n"))


def print_tick(tick: "PriceTick", count: int) -> None:
    """Basic one-liner tick — use print_tick_rich for full context."""
    ts    = tick.timestamp[11:19]
    parts = tick.market_id.split("-")
    short = f"{parts[0].upper()}/{parts[2]}"
    spread = abs(tick.yes_price - tick.no_price)
    print(
        f"  {_c(_DM, f'[{count:04d}]')} {_c(_DM, ts)}"
        f"  {_c(_CY, short)}"
        f"  YES {fmt_price(tick.yes_price)}"
        f"  NO {fmt_price(tick.no_price)}"
        f"  {_c(_DM, f'spread {spread:.4f}')}"
    )


def print_tick_rich(tick: "PriceTick", count: int, stats: TickStats) -> None:
    """
    Enhanced tick line with delta, volatility, momentum, imbalance, and sparkline.
    Call stats.update(tick) *before* this function.
    """
    ts    = tick.timestamp[11:19]
    parts = tick.market_id.split("-")
    short = f"{parts[0].upper()}/{parts[2]}"
    spread = abs(tick.yes_price - tick.no_price)

    line1 = (
        f"  {_c(_DM, f'[{count:04d}]')} {_c(_DM, ts)}"
        f"  {_c(_CY, short)}"
        f"  YES {fmt_price(tick.yes_price)} {fmt_delta(stats.delta)}"
        f"  NO {fmt_price(tick.no_price)}"
        f"  {_c(_DM, f'sprd {spread:.4f}')}"
    )
    line2 = (
        f"  {'':6}  {fmt_vol(stats.volatility)}"
        f"   {fmt_momentum(stats.momentum, stats.window)}"
        f"   {fmt_imbalance(stats.imbalance(tick.order_book))}"
        f"  {fmt_sparkline(stats.prices)}"
    )
    print(line1)
    print(line2)


def print_orderbook(order_book: "OrderBook", market_id: str, depth: int = 5) -> None:
    """
    Render a side-by-side order book for YES and NO tokens.

      YES token                      NO token
      ── Bids ──   ── Asks ──    ── Bids ──   ── Asks ──
      0.6150 x500  0.6200 x300   0.3800 x200  0.3850 x450
      ...
    """
    W = 72
    def _lvl(levels: list, i: int, side: str) -> str:
        if i >= len(levels):
            return " " * 14
        lv = levels[i]
        p = f"{lv.price:.4f}"
        s = f"x{lv.size:.0f}"
        if side == "bid":
            return _c(_GR, p) + _c(_DM, f" {s:<6}")
        else:
            return _c(_RD, p) + _c(_DM, f" {s:<6}")

    yes_bids = sorted(order_book.yes_bids, key=lambda l: l.price, reverse=True)
    yes_asks = sorted(order_book.yes_asks, key=lambda l: l.price)
    no_bids  = sorted(order_book.no_bids,  key=lambda l: l.price, reverse=True)
    no_asks  = sorted(order_book.no_asks,  key=lambda l: l.price)

    mkt = market_id.split("-")
    title = f" Order Book · {mkt[0].upper()}/{mkt[2]} "

    print()
    print(_c(_DM, f"  ┌{'─' * (W - 4)}┐"))
    # title centred
    pad_l = (W - 4 - len(title)) // 2
    pad_r = W - 4 - len(title) - pad_l
    print(_c(_DM, "  │") + " " * pad_l + _c(_B + _WH, title) + " " * pad_r + _c(_DM, "│"))
    print(_c(_DM, f"  ├{'─' * (W - 4)}┤"))

    # Column headers
    hdr = (
        f"  {_c(_DM, '│')}"
        f"  {_c(_B, '── YES Bids ──'):14}  {_c(_B, '── YES Asks ──'):14}"
        f"    {_c(_B, '── NO Bids ──'):14}  {_c(_B, '── NO Asks ──'):14}"
        f"  {_c(_DM, '│')}"
    )
    print(hdr)
    print(_c(_DM, f"  ├{'─' * (W - 4)}┤"))

    rows = max(depth, max(len(yes_bids), len(yes_asks), len(no_bids), len(no_asks), 1))
    rows = min(rows, depth)
    for i in range(rows):
        yb = _lvl(yes_bids, i, "bid")
        ya = _lvl(yes_asks, i, "ask")
        nb = _lvl(no_bids,  i, "bid")
        na = _lvl(no_asks,  i, "ask")
        raw = f"  {_strip_ansi(yb)}  {_strip_ansi(ya)}    {_strip_ansi(nb)}  {_strip_ansi(na)}"
        pad = W - 4 - len(raw)
        print(_c(_DM, "  │") + f"  {yb}  {ya}    {nb}  {na}" + " " * max(0, pad) + _c(_DM, "│"))

    print(_c(_DM, f"  └{'─' * (W - 4)}┘"))
    print()


def print_trade_opened(trade: "Trade") -> None:
    arrow = _c(_GR, "▶")
    d     = fmt_direction(trade.direction)
    price = _c(_WH, f"{trade.entry_price:.4f}")
    cost  = _c(_DM, f"(cost ${trade.shares * trade.entry_price:.2f})")
    tid   = fmt_id(trade.id)
    print(f"  {arrow} BUY   {d}  {trade.shares:g} shares @ {price}  {cost}  {tid}")


def print_trade_closed(trade: "Trade") -> None:
    pnl   = trade.pnl or 0.0
    arrow = _c(_GR if pnl >= 0 else _RD, "■")
    d     = fmt_direction(trade.direction)
    entry = _c(_DM, f"{trade.entry_price:.4f}")
    exit_ = _c(_WH, f"{trade.exit_price:.4f}")
    fc    = _c(_MG, " [force]") if trade.force_closed else ""
    print(
        f"  {arrow} CLOSE {d}  {trade.shares:g} shares  "
        f"{entry} → {exit_}  pnl {fmt_pnl(pnl)}{fc}  {fmt_id(trade.id)}"
    )


def print_rotation(tick: "MarketRotationTick") -> None:
    print()
    print(_c(_DM, "  " + "─" * 70))
    print(f"  {_c(_MG + _B, '↻ MARKET ROTATION')}")
    print(f"  {_c(_DM, 'from')}  {_c(_WH, tick.old_market_id)}")
    print(f"  {_c(_DM, 'to  ')}  {_c(_CY + _B, tick.new_market_id)}")
    print(_c(_DM, "  " + "─" * 70))


def print_summary(summary: dict) -> None:
    W  = 52
    wr = fmt_win_rate(summary.get("win_rate"))
    lp = summary.get("latest_yes_price")
    lp_s = fmt_price(lp) if lp is not None else _c(_DM, "n/a")

    def row(label: str, value: str) -> None:
        raw_len = len(label) + 2 + len(_strip_ansi(value))
        pad = " " * max(0, W - 2 - raw_len)
        print(_c(_B, "  ║") + f"  {_c(_DM, label)}  {value}{pad}" + _c(_B, "║"))

    print()
    print(_c(_B, f"  ╔{'═' * W}╗"))
    title = "  Portfolio Summary"
    print(_c(_B, "  ║") + _c(_B + _WH, title.ljust(W)) + _c(_B, "║"))
    print(_c(_B, f"  ╠{'═' * W}╣"))
    row("Cash           ", fmt_cash(summary["cash"]))
    row("Open trades    ", _c(_WH, str(summary["open_trades"])))
    row("Closed trades  ", _c(_WH, str(summary["closed_trades"])))
    print(_c(_B, f"  ╠{'═' * W}╣"))
    row("Realised PnL   ", fmt_pnl(summary["realised_pnl"]))
    row("Unrealised PnL ", fmt_pnl(summary["unrealised_pnl"]))
    row("Total PnL      ", fmt_pnl(summary["total_pnl"]))
    row("Win rate       ", wr)
    print(_c(_B, f"  ╠{'═' * W}╣"))
    row("Market         ", _c(_CY, summary["market_id"]))
    row("Latest YES     ", lp_s)
    lr = summary.get("last_rotation")
    if lr:
        fc = lr.get("force_closed_trades", [])
        row("Last rotation  ", _c(_MG, lr["new_market_id"]))
        row("  force-closed ", _c(_DM, f"{len(fc)} trade(s)"))
    print(_c(_B, f"  ╚{'═' * W}╝"))
    print()
