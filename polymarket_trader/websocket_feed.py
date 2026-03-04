from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.request
from datetime import datetime, timezone
from typing import AsyncGenerator

import certifi
import websockets

# Single SSL context used for all outbound connections.
# certifi ships its own CA bundle so this works on any Python install,
# including Python.org macOS builds that don't use the system keychain.
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

from .models import FeedEvent, Level, MarketRotationTick, OrderBook, PriceTick
from .utils import MarketSpec

logger = logging.getLogger(__name__)

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_GAMMA_URL = "https://gamma-api.polymarket.com/events?slug={}"
_PING_INTERVAL = 10  # seconds


def _best_mid(levels: list[dict]) -> float | None:
    """Return midpoint of best bid and best ask from a list of level dicts."""
    if not levels:
        return None
    # Levels from book event: list of {price, size}
    try:
        prices = [float(lv["price"]) for lv in levels if float(lv.get("size", 0)) > 0]
    except (KeyError, ValueError):
        return None
    if not prices:
        return None
    return prices[0]


def _parse_levels(raw: list[dict]) -> list[Level]:
    result = []
    for lv in raw:
        try:
            result.append(Level(price=float(lv["price"]), size=float(lv["size"])))
        except (KeyError, ValueError):
            pass
    return result


def _mid_from_book(bids: list[dict], asks: list[dict]) -> float | None:
    bids_sorted = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
    asks_sorted = sorted(asks, key=lambda x: float(x.get("price", 0)))
    best_bid = float(bids_sorted[0]["price"]) if bids_sorted else None
    best_ask = float(asks_sorted[0]["price"]) if asks_sorted else None
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / 2
    if best_bid is not None:
        return best_bid
    if best_ask is not None:
        return best_ask
    return None


class PolymarketFeed:
    def __init__(self, market_spec: MarketSpec) -> None:
        self._spec = market_spec
        # Stores per-asset_id: {"side": "YES"/"NO", "bids": [...], "asks": [...]}
        self._books: dict[str, dict] = {}
        self._token_map: dict[str, str] = {}  # asset_id -> "YES"/"NO"

    async def _resolve_token_ids(self, market_id: str) -> tuple[str, str]:
        url = _GAMMA_URL.format(market_id)
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, self._fetch_url, url)
        data = json.loads(raw)
        if not data:
            raise ValueError(f"No event found for slug {market_id!r}")
        market = data[0].get("markets", [{}])[0]
        token_ids = market.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        if len(token_ids) < 2:
            raise ValueError(
                f"Expected 2 clobTokenIds for {market_id!r}, got {token_ids}"
            )
        return str(token_ids[0]), str(token_ids[1])

    @staticmethod
    def _fetch_url(url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-trader/0.1"})
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            return resp.read()

    def _make_subscription(self, yes_id: str, no_id: str) -> str:
        return json.dumps({
            "type": "market",
            "assets_ids": [yes_id, no_id],
            "custom_feature_enabled": True,
        })

    def _build_tick(self, market_id: str) -> PriceTick | None:
        yes_id = next((k for k, v in self._token_map.items() if v == "YES"), None)
        no_id = next((k for k, v in self._token_map.items() if v == "NO"), None)

        yes_book = self._books.get(yes_id, {}) if yes_id else {}
        no_book = self._books.get(no_id, {}) if no_id else {}

        yes_bids = yes_book.get("bids", [])
        yes_asks = yes_book.get("asks", [])
        no_bids = no_book.get("bids", [])
        no_asks = no_book.get("asks", [])

        yes_price = _mid_from_book(yes_bids, yes_asks)
        no_price = _mid_from_book(no_bids, no_asks)

        # Fallback: if NO book sparse, derive from YES
        if yes_price is not None and no_price is None:
            no_price = 1.0 - yes_price
        if no_price is not None and yes_price is None:
            yes_price = 1.0 - no_price

        if yes_price is None or no_price is None:
            return None

        order_book = OrderBook(
            yes_bids=sorted(_parse_levels(yes_bids), key=lambda l: l.price, reverse=True),
            yes_asks=sorted(_parse_levels(yes_asks), key=lambda l: l.price),
            no_bids=sorted(_parse_levels(no_bids), key=lambda l: l.price, reverse=True),
            no_asks=sorted(_parse_levels(no_asks), key=lambda l: l.price),
        )
        return PriceTick(
            market_id=market_id,
            yes_price=yes_price,
            no_price=no_price,
            timestamp=datetime.now(timezone.utc).isoformat(),
            order_book=order_book,
        )

    def _handle_message(self, raw: str, market_id: str) -> PriceTick | None:
        try:
            events = json.loads(raw)
        except json.JSONDecodeError:
            return None

        if not isinstance(events, list):
            events = [events]

        updated = False
        for event in events:
            event_type = event.get("event_type", "")
            asset_id = event.get("asset_id")

            if event_type == "book" and asset_id:
                self._books.setdefault(asset_id, {})
                self._books[asset_id]["bids"] = event.get("bids", [])
                self._books[asset_id]["asks"] = event.get("asks", [])
                updated = True

            elif event_type in ("price_change", "best_bid_ask") and asset_id:
                self._books.setdefault(asset_id, {})
                # price_change carries changes list; best_bid_ask carries bid/ask
                changes = event.get("changes", [])
                for change in changes:
                    side = change.get("side", "").lower()
                    price = change.get("price")
                    size = change.get("size")
                    if side and price is not None:
                        levels = self._books[asset_id].setdefault(side + "s", [])
                        # Update or add level
                        existing = next(
                            (l for l in levels if l["price"] == price), None
                        )
                        if existing:
                            existing["size"] = size
                        else:
                            levels.append({"price": price, "size": size})
                        # Remove zero-size levels
                        self._books[asset_id][side + "s"] = [
                            l for l in self._books[asset_id][side + "s"]
                            if float(l.get("size", 0)) > 0
                        ]
                # best_bid_ask shortcut
                if "bid" in event and "ask" in event:
                    bid = event["bid"]
                    ask = event["ask"]
                    if bid:
                        self._books[asset_id]["bids"] = [
                            {"price": bid, "size": event.get("bid_size", 1)}
                        ]
                    if ask:
                        self._books[asset_id]["asks"] = [
                            {"price": ask, "size": event.get("ask_size", 1)}
                        ]
                updated = True

            elif event_type == "last_trade_price" and asset_id:
                price = event.get("price")
                if price and asset_id in self._books:
                    updated = True

        if updated:
            return self._build_tick(market_id)
        return None

    async def price_stream(self) -> AsyncGenerator[FeedEvent, None]:
        spec = self._spec
        backoff = 1

        while True:
            try:
                yes_id, no_id = await self._resolve_token_ids(spec.market_id)
                self._token_map = {yes_id: "YES", no_id: "NO"}
                self._books = {}

                async with websockets.connect(_WS_URL, ssl=_SSL_CTX) as ws:
                    backoff = 1  # reset on successful connect
                    await ws.send(self._make_subscription(yes_id, no_id))
                    logger.info("Subscribed to %s", spec.market_id)

                    async def ping_loop():
                        while True:
                            await asyncio.sleep(_PING_INTERVAL)
                            try:
                                await ws.ping()
                            except Exception:
                                break

                    async def rotation_timer() -> MarketRotationTick | None:
                        secs = spec.seconds_until_resolution - 5
                        if secs > 0:
                            await asyncio.sleep(secs)
                        return MarketRotationTick(
                            old_market_id=spec.market_id,
                            new_market_id=spec.next.market_id,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )

                    ping_task = asyncio.create_task(ping_loop())
                    rotate_task = asyncio.create_task(rotation_timer())

                    try:
                        while True:
                            recv_task = asyncio.create_task(ws.recv())
                            done, _ = await asyncio.wait(
                                {recv_task, rotate_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )

                            if rotate_task in done:
                                recv_task.cancel()
                                rotation = rotate_task.result()
                                yield rotation
                                # Advance spec and reconnect
                                spec = spec.next
                                self._spec = spec
                                break

                            msg = recv_task.result()
                            tick = self._handle_message(msg, spec.market_id)
                            if tick is not None:
                                yield tick
                    finally:
                        ping_task.cancel()
                        if not rotate_task.done():
                            rotate_task.cancel()

            except (OSError, websockets.WebSocketException) as exc:
                logger.warning("WS error: %s — reconnecting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as exc:
                logger.error("Unexpected error: %s — reconnecting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
