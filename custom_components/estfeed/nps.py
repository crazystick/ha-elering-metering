"""Elering NPS (Nord Pool spot) price client.

Wraps the public dashboard endpoint at
``https://dashboard.elering.ee/api/nps/price`` to fetch hourly EE-zone
spot prices. Maintains a process-lifetime in-memory cache keyed by
top-of-hour UTC so a single coordinator tick that walks overlapping
windows hits the network at most once per gap.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

import aiohttp

NPS_PRICE_URL = "https://dashboard.elering.ee/api/nps/price"
NPS_TIMEOUT_SECONDS = 30
# One request per at most 31 days — a single 360-day request returns ~35k
# quarter rows and risks an Elering timeout.
NPS_MAX_FETCH_DAYS = 31
# Hours whose NPS quarters may not all be settled yet. Elering publishes
# 15-min prices progressively; an hour fetched inside this horizon may only
# have a subset of its quarters available, and the hourly mean computed from
# that subset would be wrong. Hours newer than this horizon must be evicted
# from the cache before each fetch so they are re-priced once complete.
NPS_PRICE_SETTLE_HOURS = 2

_LOGGER = logging.getLogger(__name__)


class NpsError(Exception):
    """Raised when the NPS endpoint returns a non-200 status or unparseable body."""


class EleringNpsClient:
    """Async client for Elering's public NPS price endpoint.

    No auth required. Returns prices in EUR/kWh (converted from the
    endpoint's native EUR/MWh).
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # Top-of-hour UTC → EUR/kWh
        self._cache: dict[datetime, float] = {}
        # Quarter start UTC → EUR/kWh, as published (Elering's 15-min rows since Oct 2025).
        self._quarters: dict[datetime, float] = {}

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def clear_cache(self) -> None:
        """Drop every cached hour.

        Used at the start of a cost rebuild so prior partial-data entries
        (Elering publishes 15-min quarters as they settle; an hour fetched
        before all 4 quarters land would be cached at the partial mean and
        never refreshed) cannot poison the rewrite.
        """
        self._cache.clear()
        self._quarters.clear()

    def evict_after(self, cutoff: datetime) -> None:
        """Drop cached hours at or after ``cutoff`` (UTC).

        Called by the coordinator's regular tick before fetching prices so
        recent hours whose 15-min quarters were still settling at first
        fetch get re-fetched (and re-meaned) once all quarters are
        published. Without this, a partial-quarter mean cached on tick N
        would price that hour forever.
        """
        cutoff = cutoff.astimezone(UTC)
        self._cache = {hour: price for hour, price in self._cache.items() if hour < cutoff}
        self._quarters = {q: price for q, price in self._quarters.items() if q < cutoff}

    def quarter_prices(self, start: datetime, end: datetime) -> dict[datetime, float]:
        """Return cached 15-min EE prices in [start, end), keyed by quarter start UTC.

        Filled by the same requests as the hourly cache, so call
        ``async_get_prices`` for the range first. Hours published at hourly
        resolution (before Oct 2025) have only their :00 entry.
        """
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        return {q: price for q, price in self._quarters.items() if start <= q < end}

    def cache_snapshot(self, hours: list[datetime]) -> dict[str, float | None]:
        """Return cached EUR/kWh prices for the given hours, keyed by ISO string.

        Used by diagnostics to inspect what the rebuild actually stored — when
        cost statistics look wrong, comparing the cached price for a known
        problem hour against Elering's current published value pinpoints
        whether the bug is in the fetch path or downstream.
        """
        return {h.isoformat(): self._cache.get(h) for h in hours}

    async def async_get_prices(self, start: datetime, end: datetime) -> dict[datetime, float]:
        """Return hourly EE prices in [start, end) keyed by top-of-hour UTC.

        Cache-first: every hour already present in the cache is returned
        without an HTTP call. The gap (if any) is fetched in a single
        request covering the missing range. Empty ranges return ``{}``
        without hitting the network.
        """
        start = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        end = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        if end <= start:
            return {}

        wanted: list[datetime] = []
        cursor = start
        while cursor < end:
            wanted.append(cursor)
            cursor += timedelta(hours=1)

        missing = [h for h in wanted if h not in self._cache]
        if missing:
            await self._fetch_range(missing[0], missing[-1] + timedelta(hours=1))

        return {h: self._cache[h] for h in wanted if h in self._cache}

    async def _fetch_range(self, start: datetime, end: datetime) -> None:
        """Fetch [start, end) in bounded chunks, merging into the cache.

        Chunking lives inside the client so no caller can accidentally
        issue a single request spanning months of quarter-hour rows. Chunks
        fetched before a failure remain cached.
        """
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=NPS_MAX_FETCH_DAYS), end)
            await self._fetch_chunk(cursor, chunk_end)
            cursor = chunk_end

    async def _fetch_chunk(self, start: datetime, end: datetime) -> None:
        params = {
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        }
        try:
            async with self._session.get(
                NPS_PRICE_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=NPS_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    raise NpsError(f"NPS returned status {resp.status}")
                payload = await resp.json(content_type=None)
        except ValueError as err:
            raise NpsError("NPS returned invalid JSON") from err
        except aiohttp.ClientError as err:
            raise NpsError(f"NPS request failed: {err}") from err
        except TimeoutError as err:
            raise NpsError(f"NPS request timed out: {err}") from err

        # Elering switched to 15-min resolution in 2025: each hour now carries
        # up to 4 quarter rows. Aggregate to the hourly mean before caching —
        # writing each row directly would let the last :45 quarter overwrite
        # the others and badly mis-represent volatile hours.
        try:
            if not isinstance(payload, dict) or payload.get("success") is False:
                raise ValueError("Unsuccessful price response")
            rows = payload["data"]["ee"]
            if not isinstance(rows, list):
                raise TypeError("Expected a list of prices")
            buckets: dict[datetime, list[float]] = {}
            quarters: dict[datetime, float] = {}
            for row in rows:
                ts = row.get("timestamp")
                price_eur_per_mwh = row.get("price")
                if ts is None or price_eur_per_mwh is None:
                    continue
                price = float(price_eur_per_mwh)
                if not math.isfinite(price):
                    raise ValueError("Non-finite price")
                timestamp = datetime.fromtimestamp(int(ts), tz=UTC)
                if not start <= timestamp < end:
                    continue
                hour = timestamp.replace(minute=0, second=0, microsecond=0)
                buckets.setdefault(hour, []).append(price)
                quarters[timestamp] = price / 1000.0
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError) as err:
            raise NpsError("NPS returned invalid price data") from err
        for hour, prices in buckets.items():
            self._cache[hour] = (sum(prices) / len(prices)) / 1000.0
        self._quarters.update(quarters)
