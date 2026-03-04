from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import ClassVar

INTERVAL_SECONDS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}

_MARKET_RE = re.compile(
    r"^(?P<asset>[a-z]+)-updown-(?P<interval>\d+[mhd])-(?P<ts>\d+)$"
)


@dataclass(frozen=True)
class MarketSpec:
    asset: str
    interval_slug: str
    resolution_ts: int

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS[self.interval_slug]

    @property
    def market_id(self) -> str:
        return f"{self.asset}-updown-{self.interval_slug}-{self.resolution_ts}"

    @property
    def next(self) -> "MarketSpec":
        return MarketSpec(
            asset=self.asset,
            interval_slug=self.interval_slug,
            resolution_ts=self.resolution_ts + self.interval_seconds,
        )

    @property
    def seconds_until_resolution(self) -> float:
        # resolution_ts is the window START; market resolves at start + interval
        return self.resolution_ts + self.interval_seconds - time.time()


class MarketClock:
    @staticmethod
    def parse(market_id: str) -> MarketSpec:
        m = _MARKET_RE.match(market_id)
        if not m:
            raise ValueError(
                f"Invalid market_id format: {market_id!r}. "
                "Expected: <asset>-updown-<interval>-<unix_ts>"
            )
        return MarketSpec(
            asset=m.group("asset"),
            interval_slug=m.group("interval"),
            resolution_ts=int(m.group("ts")),
        )

    @staticmethod
    def current(asset: str, interval_slug: str) -> MarketSpec:
        if interval_slug not in INTERVAL_SECONDS:
            raise ValueError(
                f"Unknown interval {interval_slug!r}. "
                f"Valid: {list(INTERVAL_SECONDS)}"
            )
        interval = INTERVAL_SECONDS[interval_slug]
        now = int(time.time())
        # Current window start (Polymarket slugs use the window START timestamp)
        resolution_ts = (now // interval) * interval
        return MarketSpec(
            asset=asset,
            interval_slug=interval_slug,
            resolution_ts=resolution_ts,
        )
