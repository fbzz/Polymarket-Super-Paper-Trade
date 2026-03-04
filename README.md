# polymarket-trader

A Python paper trading library for [Polymarket](https://polymarket.com) prediction markets.
Connect to any market via WebSocket, trade YES/NO positions with simulated cash, and let the library handle reconnection, market rotation, and crash-safe state persistence.

![alt text](image.png)

```python
trader = PaperTrader(asset="btc", interval="5m", initial_cash=500.0)

async def on_tick(event):
    if isinstance(event, PriceTick):
        trade = trader.buy("YES", shares=10)

asyncio.run(trader.stream(on_tick))
```

---

## Table of Contents

- [Requirements & Installation](#requirements--installation)
- [How It Works](#how-it-works)
- [Quickstart](#quickstart)
- [Core Concepts](#core-concepts)
  - [Market IDs](#market-ids)
  - [YES / NO pricing](#yes--no-pricing)
  - [Market rotation](#market-rotation)
  - [Crash recovery](#crash-recovery)
- [Using in Your Project](#using-in-your-project)
  - [Install from source](#install-from-source)
  - [Minimal integration](#minimal-integration)
  - [What data arrives on every tick](#what-data-arrives-on-every-tick)
  - [Reading the order book](#reading-the-order-book)
  - [Computing signals from order book data](#computing-signals-from-order-book-data)
  - [Rolling statistics with TickStats](#rolling-statistics-with-tickstats)
  - [Accessing trade history and portfolio state](#accessing-trade-history-and-portfolio-state)
  - [Using the raw feed without PaperTrader](#using-the-raw-feed-without-papertrader)
  - [Running multiple markets in parallel](#running-multiple-markets-in-parallel)
  - [Building a strategy class](#building-a-strategy-class)
  - [Integrating with FastAPI](#integrating-with-fastapi)
  - [Writing data to a file or database](#writing-data-to-a-file-or-database)
- [API Reference](#api-reference)
  - [PaperTrader](#papertrader)
  - [PriceTick](#pricetick)
  - [MarketRotationTick](#marketrotationtick)
  - [OrderBook & Level](#orderbook--level)
  - [Trade](#trade)
  - [Portfolio](#portfolio)
  - [MarketSpec & MarketClock](#marketspec--marketclock)
  - [TickStats](#tickstats)
  - [Display utilities](#display-utilities)
  - [Exceptions](#exceptions)
- [State File Schema](#state-file-schema)
- [Extending the Library](#extending-the-library)
- [Running Tests](#running-tests)

---

## Requirements & Installation

- Python **3.11+**
- `websockets >= 12`
- `certifi` (CA bundle — works on all Python installs including macOS Python.org builds)

```bash
# Development install (includes pytest)
pip install -e ".[dev]"

# Production only
pip install -e .
```

---

## How It Works

```
  ┌─────────────────────────────────────────────────────────┐
  │                    Your Script / App                    │
  │   trader = PaperTrader(asset="btc", interval="5m")      │
  │   asyncio.run(trader.stream(on_tick))                   │
  └────────────────────┬────────────────────────────────────┘
                       │  PriceTick | MarketRotationTick
  ┌────────────────────▼────────────────────────────────────┐
  │                   PaperTrader                           │
  │  • tracks latest_price          • saves state atomically│
  │  • auto-closes on rotation      • crash-safe JSON store │
  └────────────────────┬────────────────────────────────────┘
                       │
  ┌────────────────────▼────────────────────────────────────┐
  │                  PolymarketFeed                         │
  │  • resolves YES/NO token IDs (Gamma API)                │
  │  • maintains WebSocket + PING every 10s                 │
  │  • fires rotation tick 5s before window close           │
  │  • reconnect with backoff: 1s → 2s → 4s … cap 60s      │
  └────────────────────┬────────────────────────────────────┘
                       │
              wss://ws-subscriptions-clob.polymarket.com
```

Every `buy()` and `close()` is written atomically to disk. If your process dies, the next run picks up exactly where it left off.

---

## Quickstart

### BTC 5-minute

```python
import asyncio
from polymarket_trader import PaperTrader
from polymarket_trader.models import PriceTick, MarketRotationTick

trader = PaperTrader(asset="btc", interval="5m", initial_cash=500.0)
tick_count = 0

async def on_tick(event):
    global tick_count

    if isinstance(event, MarketRotationTick):
        print(f"Rotated → {event.new_market_id}")
        return

    tick_count += 1
    tick: PriceTick = event
    print(f"[{tick_count}] YES={tick.yes_price:.4f}  NO={tick.no_price:.4f}")

    if tick_count == 1:
        trade = trader.buy("YES", shares=10)
    if tick_count == 10:
        for t in trader.close_all():
            print(f"Closed pnl={t.pnl:+.4f}")

asyncio.run(trader.stream(on_tick))
```

### ETH 15-minute

```python
trader = PaperTrader(asset="eth", interval="15m", initial_cash=500.0)
asyncio.run(trader.stream(on_tick))   # same on_tick works unchanged
```

### Pin to a specific window

```python
trader = PaperTrader(market_id="btc-updown-5m-1700000300")
```

### Trade without streaming (backtesting / testing)

```python
trader = PaperTrader(asset="btc", interval="5m")
trade  = trader.buy("YES", shares=10, price=0.62)
closed = trader.close(trade.id, price=0.71)
print(f"PnL: {closed.pnl:+.4f}")   # PnL: +0.9000
```

---

## Core Concepts

### Market IDs

Polymarket up/down markets are identified by:

```
{asset}-updown-{interval}-{window_start_unix_ts}
```

The timestamp is the **start** of the window (not the end). The market resolves `interval` seconds later.

```
btc-updown-5m-1700000300    # BTC, 5-min window starting at 1700000300 UTC
eth-updown-15m-1700001200   # ETH, 15-min window
btc-updown-1h-1700003600    # BTC, 1-hour window
```

`MarketClock.current(asset, interval)` computes the currently active window start for you.

### YES / NO Pricing

Each binary market has two independently priced CLOB tokens. Prices are 0–1 and roughly sum to 1:

- **YES price** ≈ probability that the asset goes **up** in this window
- **NO price**  ≈ probability that the asset goes **down**

The library reports the **midpoint** of the best bid and best ask for each token:

```
yes_price = (best_yes_bid + best_yes_ask) / 2
no_price  = (best_no_bid  + best_no_ask)  / 2
```

If one side is sparse, the missing price falls back to `1 - other_price`.

**PnL at close:**

| Direction | Formula |
|-----------|---------|
| YES | `(exit − entry) × shares` |
| NO  | `(entry − exit) × shares` |

### Market Rotation

When a window expires, the library automatically:

1. Fires `MarketRotationTick` to your callback (~5 s before expiry)
2. Force-closes all open trades at the last known price (if `auto_close_on_rotation=True`)
3. Advances to the next window and reconnects the WebSocket

```python
# Manage rotation yourself
trader = PaperTrader(asset="btc", interval="5m", auto_close_on_rotation=False)

async def on_tick(event):
    if isinstance(event, MarketRotationTick):
        # decide what to do with open positions
        trader.close_all()
```

Force-closed trades have `trade.force_closed = True` and appear in `summary()["last_rotation"]`.

### Crash Recovery

State is written atomically using `.tmp` → `os.replace()` after every `buy()` and `close()`. On restart, `StateManager.load()` picks up the last committed portfolio automatically.

---

## Using in Your Project

### Install from source

```bash
# Into your virtual environment
pip install -e /path/to/polymarket-trader

# Or copy the package directory
cp -r polymarket-trader/polymarket_trader your_project/
```

Then import:

```python
from polymarket_trader import PaperTrader, TickStats
from polymarket_trader.models import PriceTick, MarketRotationTick, OrderBook, Level
from polymarket_trader.utils import MarketClock, MarketSpec
from polymarket_trader.websocket_feed import PolymarketFeed
```

---

### Minimal integration

The only entry point you need:

```python
import asyncio
from polymarket_trader import PaperTrader
from polymarket_trader.models import PriceTick, MarketRotationTick

trader = PaperTrader(asset="btc", interval="5m")

async def on_tick(event):
    if isinstance(event, PriceTick):
        ...   # your logic here
    elif isinstance(event, MarketRotationTick):
        ...   # window expired

asyncio.run(trader.stream(on_tick))
```

`on_tick` can be **sync or async** — the library detects this automatically.

---

### What data arrives on every tick

Every `PriceTick` carries:

```python
@dataclass
class PriceTick:
    market_id:  str        # "btc-updown-5m-1700000300"
    yes_price:  float      # YES midpoint  (0.0 – 1.0)
    no_price:   float      # NO midpoint   (0.0 – 1.0)
    timestamp:  str        # ISO 8601 UTC  "2026-03-04T12:00:01+00:00"
    order_book: OrderBook  # full CLOB snapshot — all 4 sides
```

Access any field directly:

```python
async def on_tick(event):
    if isinstance(event, PriceTick):
        print(event.yes_price)          # 0.6142
        print(event.no_price)           # 0.3858
        print(event.timestamp)          # "2026-03-04T12:00:01+00:00"
        print(event.market_id)          # "btc-updown-5m-1700000300"

        # implied spread
        spread = event.yes_price - event.no_price
        print(f"YES premium over NO: {spread:+.4f}")
```

---

### Reading the order book

The full CLOB is on every tick as `event.order_book`. Each side is a `list[Level]` (price, size pairs):

```python
async def on_tick(event):
    if not isinstance(event, PriceTick):
        return

    ob = event.order_book

    # --- 4 sides ---
    ob.yes_bids   # list[Level] — YES buyers, best price first
    ob.yes_asks   # list[Level] — YES sellers, best price first
    ob.no_bids    # list[Level] — NO buyers
    ob.no_asks    # list[Level] — NO sellers

    # --- best bid / ask for YES ---
    if ob.yes_bids:
        best_bid = ob.yes_bids[0]
        print(f"Best YES bid: {best_bid.price:.4f} x {best_bid.size:.0f} shares")

    if ob.yes_asks:
        best_ask = ob.yes_asks[0]
        print(f"Best YES ask: {best_ask.price:.4f} x {best_ask.size:.0f} shares")

    # --- top-3 levels ---
    for level in ob.yes_bids[:3]:
        print(f"  bid {level.price:.4f}  size {level.size:.0f}")

    # --- total liquidity in top 5 levels ---
    bid_liquidity = sum(l.size for l in ob.yes_bids[:5])
    ask_liquidity = sum(l.size for l in ob.yes_asks[:5])
    print(f"YES liquidity — bids: {bid_liquidity:.0f}  asks: {ask_liquidity:.0f}")

    # --- bid-ask spread ---
    if ob.yes_bids and ob.yes_asks:
        spread = ob.yes_asks[0].price - ob.yes_bids[0].price
        print(f"YES spread: {spread:.4f}")
```

---

### Computing signals from order book data

Everything is plain Python — compute whatever you need directly:

```python
from polymarket_trader.models import OrderBook, Level

def bid_ask_spread(bids: list[Level], asks: list[Level]) -> float | None:
    """Spread between best bid and best ask."""
    if not bids or not asks:
        return None
    return asks[0].price - bids[0].price

def book_imbalance(bids: list[Level], asks: list[Level], depth: int = 5) -> float | None:
    """
    Signed imbalance in [-1, +1].
    +1.0 → all size on bid side (buying pressure)
    -1.0 → all size on ask side (selling pressure)
    """
    bid_vol = sum(l.size for l in bids[:depth])
    ask_vol = sum(l.size for l in asks[:depth])
    total = bid_vol + ask_vol
    if total == 0:
        return None
    return (bid_vol - ask_vol) / total

def weighted_mid(bids: list[Level], asks: list[Level]) -> float | None:
    """
    Size-weighted midpoint — pulls toward the heavier side.
    More accurate than a simple (bid + ask) / 2.
    """
    if not bids or not asks:
        return None
    bb, ba = bids[0], asks[0]
    return (bb.price * ba.size + ba.price * bb.size) / (bb.size + ba.size)

def vwap(levels: list[Level], depth: int = 10) -> float | None:
    """Volume-weighted average price across top N levels."""
    vol = sum(l.size for l in levels[:depth])
    if vol == 0:
        return None
    return sum(l.price * l.size for l in levels[:depth]) / vol


# --- use them in your callback ---
async def on_tick(event):
    if not isinstance(event, PriceTick):
        return
    ob = event.order_book

    spread  = bid_ask_spread(ob.yes_bids, ob.yes_asks)
    imb     = book_imbalance(ob.yes_bids, ob.yes_asks)
    wmid    = weighted_mid(ob.yes_bids, ob.yes_asks)
    yes_vwap = vwap(ob.yes_bids)

    print(f"spread={spread:.4f}  imb={imb:+.2f}  wmid={wmid:.4f}  vwap={yes_vwap:.4f}")
```

---

### Rolling statistics with TickStats

`TickStats` tracks a rolling window of prices and exposes volatility, momentum, and bid/ask imbalance. Call `.update(tick)` before reading any property.

```python
from polymarket_trader import PaperTrader, TickStats
from polymarket_trader.models import PriceTick

trader = PaperTrader(asset="btc", interval="5m")
stats  = TickStats(window=20)   # rolling window of 20 ticks

async def on_tick(event):
    if not isinstance(event, PriceTick):
        return

    stats.update(event)   # always update first

    # --- available after >= 2 ticks ---
    stats.delta           # float | None — price change vs previous tick
    stats.prices          # list[float]  — full rolling window

    # --- available after >= 3 ticks ---
    stats.volatility      # float | None — rolling std-dev of tick-to-tick changes
    stats.momentum        # float | None — total price drift over the window

    # --- requires order book ---
    imb = stats.imbalance(event.order_book)   # float | None in [-1, +1]

    print(
        f"delta={stats.delta:+.4f}  "
        f"vol={stats.volatility:.4f}  "
        f"mom={stats.momentum:+.4f}  "
        f"imb={imb:+.2f}"
    )

asyncio.run(trader.stream(on_tick))
```

You can also compute volatility or momentum manually from `stats.prices`:

```python
import statistics

prices  = stats.prices           # e.g. [0.51, 0.52, 0.515, ...]
returns = [prices[i] - prices[i-1] for i in range(1, len(prices))]
vol     = statistics.stdev(returns) if len(returns) >= 2 else None
```

---

### Accessing trade history and portfolio state

The portfolio is always available at `trader.portfolio` and updates in real time:

```python
p = trader.portfolio

# --- cash ---
print(p.cash)              # 437.50  (remaining cash)

# --- open positions ---
for t in p.open_trades:
    current_price = trader.latest_price.yes_price if trader.latest_price else t.entry_price
    unreal = t.unrealised(current_price)
    print(f"  {t.id[:8]}  {t.direction}  {t.shares}sh @ {t.entry_price:.4f}  unreal {unreal:+.4f}")

# --- closed positions ---
for t in p.closed_trades:
    print(f"  {t.id[:8]}  {t.direction}  pnl {t.pnl:+.4f}  {'[force]' if t.force_closed else ''}")

# --- aggregates ---
print(p.realised_pnl)      # sum of pnl from closed trades
print(p.win_rate)          # 0.75  (or None if no closed trades)

# --- full snapshot dict ---
s = trader.summary()
print(s["cash"])
print(s["total_pnl"])
print(s["win_rate"])
print(s["latest_yes_price"])
print(s["last_rotation"])   # dict | None — info on last window change
```

Inspecting a single trade:

```python
trade = trader.buy("YES", shares=10)

trade.id            # "a1b2c3d4-..."  (UUID)
trade.market_id     # "btc-updown-5m-1700000300"
trade.direction     # "YES"
trade.shares        # 10.0
trade.entry_price   # 0.6200
trade.entry_time    # "2026-03-04T12:00:01+00:00"
trade.is_open       # True
trade.exit_price    # None (until closed)
trade.exit_time     # None
trade.pnl           # None (until closed)
trade.force_closed  # False

# unrealised PnL at any price
print(trade.unrealised(0.65))   # +0.3000
```

---

### Using the raw feed without PaperTrader

If you only need the price/orderbook stream and no trading layer:

```python
import asyncio
from polymarket_trader.utils import MarketClock
from polymarket_trader.websocket_feed import PolymarketFeed
from polymarket_trader.models import PriceTick, MarketRotationTick

spec = MarketClock.current("btc", "5m")
feed = PolymarketFeed(spec)

async def main():
    async for event in feed.price_stream():
        if isinstance(event, PriceTick):
            ob = event.order_book
            print(
                f"YES {event.yes_price:.4f}  "
                f"NO {event.no_price:.4f}  "
                f"best_bid {ob.yes_bids[0].price if ob.yes_bids else 'n/a'}"
            )
        elif isinstance(event, MarketRotationTick):
            print(f"Rotated → {event.new_market_id}")

asyncio.run(main())
```

The feed handles reconnect and rotation automatically. `price_stream()` is an `AsyncGenerator[FeedEvent, None]` that runs forever.

---

### Running multiple markets in parallel

Run separate traders as concurrent async tasks:

```python
import asyncio
from polymarket_trader import PaperTrader
from polymarket_trader.models import PriceTick

btc = PaperTrader(asset="btc", interval="5m",  state_file="state_btc.json")
eth = PaperTrader(asset="eth", interval="15m", state_file="state_eth.json")

async def on_btc(event):
    if isinstance(event, PriceTick):
        print(f"BTC  YES={event.yes_price:.4f}")

async def on_eth(event):
    if isinstance(event, PriceTick):
        print(f"ETH  YES={event.yes_price:.4f}")

async def main():
    await asyncio.gather(
        btc.stream(on_btc),
        eth.stream(on_eth),
    )

asyncio.run(main())
```

Each trader has its own state file so portfolios stay isolated.

---

### Building a strategy class

A clean pattern for encapsulating strategy logic:

```python
import asyncio
from polymarket_trader import PaperTrader, TickStats
from polymarket_trader.models import PriceTick, MarketRotationTick

class MomentumStrategy:
    def __init__(self, asset: str, interval: str, cash: float = 500.0):
        self.trader = PaperTrader(
            asset=asset,
            interval=interval,
            initial_cash=cash,
            state_file=f"{asset}_{interval}_state.json",
        )
        self.stats  = TickStats(window=20)
        self._trade = None

    async def on_tick(self, event):
        if isinstance(event, MarketRotationTick):
            self._trade = None
            return

        if not isinstance(event, PriceTick):
            return

        self.stats.update(event)
        mom = self.stats.momentum
        vol = self.stats.volatility

        # need enough history
        if mom is None or vol is None:
            return

        # entry: strong upward momentum, low volatility, no open position
        if mom > 0.02 and vol < 0.003 and self._trade is None:
            self._trade = self.trader.buy("YES", shares=20)

        # exit: momentum fades or reverses
        elif self._trade is not None and mom < 0.005:
            self.trader.close(self._trade.id)
            self._trade = None

    def run(self):
        asyncio.run(self.trader.stream(self.on_tick))


if __name__ == "__main__":
    MomentumStrategy(asset="btc", interval="5m").run()
```

---

### Integrating with FastAPI

Expose live market data and portfolio state over HTTP while streaming in the background:

```python
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from polymarket_trader import PaperTrader
from polymarket_trader.models import PriceTick, MarketRotationTick

trader = PaperTrader(asset="btc", interval="5m")

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(trader.stream(on_tick))
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

async def on_tick(event):
    pass  # trader.latest_price updates automatically

@app.get("/price")
def get_price():
    lp = trader.latest_price
    return {
        "yes": lp.yes_price if lp else None,
        "no":  lp.no_price  if lp else None,
        "ts":  lp.timestamp if lp else None,
    }

@app.get("/portfolio")
def get_portfolio():
    return trader.summary()

@app.post("/buy")
def buy(direction: str, shares: float):
    trade = trader.buy(direction, shares=shares)
    return {"id": trade.id, "entry_price": trade.entry_price}

@app.post("/close/{trade_id}")
def close(trade_id: str):
    trade = trader.close(trade_id)
    return {"pnl": trade.pnl}
```

---

### Writing data to a file or database

Collect every tick into a CSV or send to a database:

```python
import asyncio, csv, sys
from polymarket_trader import PaperTrader
from polymarket_trader.models import PriceTick

trader = PaperTrader(asset="btc", interval="5m")

writer = csv.writer(sys.stdout)
writer.writerow(["timestamp", "market_id", "yes_price", "no_price",
                 "yes_bid", "yes_ask", "no_bid", "no_ask",
                 "yes_bid_size", "yes_ask_size"])

async def on_tick(event):
    if not isinstance(event, PriceTick):
        return
    ob = event.order_book
    writer.writerow([
        event.timestamp,
        event.market_id,
        event.yes_price,
        event.no_price,
        ob.yes_bids[0].price if ob.yes_bids else "",
        ob.yes_asks[0].price if ob.yes_asks else "",
        ob.no_bids[0].price  if ob.no_bids  else "",
        ob.no_asks[0].price  if ob.no_asks  else "",
        ob.yes_bids[0].size  if ob.yes_bids else "",
        ob.yes_asks[0].size  if ob.yes_asks else "",
    ])

asyncio.run(trader.stream(on_tick))
```

Redirect to a file:

```bash
python collect.py > btc_5m_ticks.csv
```

---

## API Reference

### `PaperTrader`

```python
PaperTrader(
    market_id: str | None = None,       # e.g. "btc-updown-5m-1700000300"
    *,
    asset: str | None = None,           # e.g. "btc", "eth", "sol"
    interval: str | None = None,        # "5m" | "15m" | "1h" | "1d"
    initial_cash: float = 1000.0,
    state_file: str = "paper_trader_state.json",
    auto_close_on_rotation: bool = True,
)
```

Provide either `market_id` **or** both `asset` + `interval`.

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `market_id` | `str` | Current market ID string |
| `portfolio` | `Portfolio` | Live portfolio (updates after every trade) |
| `latest_price` | `PriceTick \| None` | Last received tick from the feed |

#### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `buy(direction, shares, price=None)` | `Trade` | Open a position |
| `close(trade_id, price=None)` | `Trade` | Close a position by ID |
| `close_all(price=None)` | `list[Trade]` | Close every open position |
| `summary()` | `dict` | Full portfolio snapshot |
| `async stream(on_tick)` | `None` | Start the WebSocket feed (runs forever) |

---

### `PriceTick`

Emitted for every orderbook update. Fired multiple times per second during active markets.

```python
@dataclass
class PriceTick:
    market_id:  str        # "btc-updown-5m-1700000300"
    yes_price:  float      # YES token midpoint  — range [0, 1]
    no_price:   float      # NO token midpoint   — range [0, 1]
    timestamp:  str        # ISO 8601 UTC
    order_book: OrderBook  # full CLOB snapshot (all 4 sides)
```

---

### `MarketRotationTick`

Emitted once, approximately 5 seconds before the active window closes.
If `auto_close_on_rotation=True`, all open trades are already closed when your callback receives this.

```python
@dataclass
class MarketRotationTick:
    old_market_id: str   # expiring window
    new_market_id: str   # next window (already the active subscription)
    timestamp: str       # ISO 8601 UTC
```

---

### `OrderBook` & `Level`

```python
@dataclass
class OrderBook:
    yes_bids: list[Level]   # YES buyers,  sorted best price first
    yes_asks: list[Level]   # YES sellers, sorted best price first
    no_bids:  list[Level]   # NO buyers
    no_asks:  list[Level]   # NO sellers

@dataclass
class Level:
    price: float   # 0.0 – 1.0
    size:  float   # shares available at this price
```

Accessing depth:

```python
ob.yes_bids[0]           # best bid  (Level)
ob.yes_asks[0]           # best ask
ob.yes_bids[0].price     # e.g. 0.6150
ob.yes_bids[0].size      # e.g. 500.0
ob.yes_bids[:5]          # top 5 bid levels
```

---

### `Trade`

```python
@dataclass(slots=True)
class Trade:
    id:           str                     # UUID4
    market_id:    str
    direction:    Literal["YES", "NO"]
    shares:       float
    entry_price:  float                   # price paid at open
    entry_time:   str                     # ISO 8601

    exit_price:   float | None            # None while open
    exit_time:    str | None
    pnl:          float | None            # None while open
    force_closed: bool                    # True if closed by auto-rotation
```

#### Computed

| Attribute / Method | Description |
|--------------------|-------------|
| `trade.is_open` | `True` if `exit_price is None` |
| `trade.unrealised(price)` | Open PnL at a given price: `(price − entry) × shares` for YES, `(entry − price) × shares` for NO |

---

### `Portfolio`

```python
@dataclass
class Portfolio:
    cash:       float
    trades:     list[Trade]
    created_at: str
    updated_at: str
```

#### Computed properties

| Property | Type | Description |
|----------|------|-------------|
| `open_trades` | `list[Trade]` | All trades with `exit_price is None` |
| `closed_trades` | `list[Trade]` | All trades with an exit price |
| `realised_pnl` | `float` | Sum of `pnl` across all closed trades |
| `total_pnl` | `float` | Same as `realised_pnl` (unrealised requires current prices) |
| `win_rate` | `float \| None` | Fraction of closed trades where `pnl > 0`; `None` if no closed trades |

#### `summary(current_prices=None) → dict`

Accepts an optional `{market_id: yes_price}` map to compute unrealised PnL. Called automatically by `PaperTrader.summary()`.

---

### `MarketSpec` & `MarketClock`

#### `MarketSpec`

Frozen dataclass representing one market window.

```python
spec = MarketSpec(asset="btc", interval_slug="5m", resolution_ts=1700000300)

spec.market_id                # "btc-updown-5m-1700000300"
spec.interval_seconds         # 300
spec.next                     # MarketSpec for the next window
spec.seconds_until_resolution # seconds until this window closes (float)
```

`resolution_ts` is the **start** of the window. The window closes at `resolution_ts + interval_seconds`.

#### `MarketClock`

```python
# Parse a market_id string
spec = MarketClock.parse("btc-updown-5m-1700000300")

# Get the currently active window
spec = MarketClock.current("btc", "5m")
spec = MarketClock.current("eth", "15m")
```

#### `INTERVAL_SECONDS`

```python
from polymarket_trader.utils import INTERVAL_SECONDS
# {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
```

---

### `TickStats`

Rolling market statistics tracker. Zero dependencies, works offline.

```python
from polymarket_trader import TickStats

stats = TickStats(window=20)   # rolling window size (default: 20)
```

#### Usage

```python
stats.update(tick)          # call once per PriceTick, before reading properties
```

#### Properties

| Property | Type | Available after | Description |
|----------|------|-----------------|-------------|
| `prices` | `list[float]` | 1 tick | Rolling window of YES prices |
| `delta` | `float \| None` | 2 ticks | Change vs previous tick |
| `volatility` | `float \| None` | 3 ticks | Std-dev of tick-to-tick changes |
| `momentum` | `float \| None` | 2 ticks | Total price drift over the window |

#### Method

```python
stats.imbalance(order_book)  # → float | None
```

Returns signed bid/ask size imbalance using top-3 YES levels:
`+1.0` = all size on bids (buy pressure), `-1.0` = all on asks (sell pressure).

---

### Display utilities

Import from `polymarket_trader` directly:

```python
from polymarket_trader import (
    TickStats,
    fmt_pnl,           # format a PnL value with colour and sign
    fmt_price,         # format a 0-1 price with colour
    print_startup,     # header block on startup
    print_tick,        # basic one-line tick
    print_tick_rich,   # two-line tick with delta/vol/momentum/imbalance/sparkline
    print_orderbook,   # side-by-side YES/NO order book panel
    print_trade_opened,
    print_trade_closed,
    print_rotation,
    print_summary,
)
```

#### `print_tick_rich(tick, count, stats)`

```
  [0021] 12:20:00  BTC/5m  YES 0.5720 ▲+0.0020  NO 0.4280  sprd 0.0100
                   vol 0.0034   mom(20) +0.0620   imb +0.20 █░░░░░░  ▁▂▃▄▅▆▇
```

#### `print_orderbook(order_book, market_id, depth=5)`

```
  ┌─────────────── Order Book · BTC/5m ───────────────┐
  │  ── YES Bids ──  ── YES Asks ──   ── NO Bids ──  ── NO Asks ──  │
  │  0.6150 x500    0.6200 x300     0.3800 x200   0.3850 x450       │
  │  0.6100 x800    0.6250 x600     0.3750 x400   0.3900 x300       │
  └───────────────────────────────────────────────────┘
```

#### `fmt_pnl(value)` / `fmt_price(price)`

Inline formatters — return coloured strings for use in your own output:

```python
print(f"PnL: {fmt_pnl(trade.pnl)}")        # green +0.7000 or red -0.3000
print(f"YES: {fmt_price(tick.yes_price)}")  # green ≥0.65, red ≤0.35, yellow otherwise
```

Colours auto-disable when `NO_COLOR` env var is set or stdout is not a TTY.

---

### Exceptions

All inherit from `PolymarketTraderError`.

```python
from polymarket_trader import (
    PolymarketTraderError,    # base — catch all library errors
    NoPriceAvailableError,    # buy/close with no feed price and no explicit price=
    InsufficientFundsError,   # shares × price > portfolio.cash
    TradeNotFoundError,       # trade_id not in portfolio
    TradeAlreadyClosedError,  # closing a trade that is already closed
    MarketResolutionError,    # rotation / resolution failure
)
```

Recommended pattern:

```python
from polymarket_trader import InsufficientFundsError, NoPriceAvailableError

async def on_tick(event):
    if isinstance(event, PriceTick):
        try:
            trader.buy("YES", shares=50)
        except InsufficientFundsError as e:
            print(f"Skipping — {e}")
        except NoPriceAvailableError:
            pass   # shouldn't happen inside on_tick, but safe to guard
```

---

## State File Schema

`paper_trader_state.json` is human-readable JSON, safe to inspect or edit:

```json
{
  "cash": 943.20,
  "trades": [
    {
      "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "market_id": "btc-updown-5m-1700000300",
      "direction": "YES",
      "shares": 10.0,
      "entry_price": 0.5800,
      "entry_time": "2026-01-01T00:02:14+00:00",
      "exit_price": 0.6500,
      "exit_time": "2026-01-01T00:04:51+00:00",
      "pnl": 0.7000,
      "force_closed": false
    }
  ],
  "created_at": "2026-01-01T00:00:00+00:00",
  "updated_at": "2026-01-01T00:04:55+00:00"
}
```

**Reset state:**
```bash
rm paper_trader_state.json
```

**Multiple strategies — separate state files:**
```python
btc_trader = PaperTrader(asset="btc", interval="5m",  state_file="btc.json")
eth_trader = PaperTrader(asset="eth", interval="15m", state_file="eth.json")
```

**Load state programmatically:**
```python
from polymarket_trader.state import StateManager

sm = StateManager("paper_trader_state.json")
portfolio = sm.load()
print(portfolio.cash)
print(portfolio.realised_pnl)
for trade in portfolio.closed_trades:
    print(trade.id, trade.pnl)
```

---

## Extending the Library

### Add a new interval

One line in `polymarket_trader/utils.py`:

```python
INTERVAL_SECONDS: dict[str, int] = {
    "5m":  300,
    "15m": 900,
    "30m": 1800,   # ← add this
    "1h":  3600,
    "1d":  86400,
}
```

`MarketClock.current("btc", "30m")` and auto-rotation both work immediately.

### Add a new asset

No code changes. Pass whatever slug Polymarket uses for the market:

```python
trader = PaperTrader(asset="sol", interval="5m")
trader = PaperTrader(asset="xrp", interval="15m")
```

Token IDs are resolved from the Gamma API at runtime.

---

## Running Tests

```bash
pytest tests/ -v
```

All 33 tests run without network access. `StateManager` and WebSocket calls are fully mocked.

```
tests/test_paper_trader.py::TestMarketSpec::test_market_id_roundtrip     PASSED
tests/test_paper_trader.py::TestMarketSpec::test_interval_seconds        PASSED
...
tests/test_paper_trader.py::TestForceClose::test_force_close_updates_last_rotation PASSED

33 passed in 0.07s
```

### Live demos

```bash
python examples/btc_5min_demo.py
python examples/eth_15min_demo.py
```
