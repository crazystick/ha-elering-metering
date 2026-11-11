"""Pure pricing helpers: VAT schedule, tariff math and cost row construction."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Self
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import StatisticData

from .api import AccountingInterval, interval_value
from .const import (
    CONF_MARGIN_EUR_PER_KWH,
    CONF_PRODUCTION_FEE_EUR_PER_KWH,
    CONF_PRODUCTION_VAT,
    CONF_VAT_MODE,
    CONF_VAT_PERCENT,
    DEFAULT_MARGIN_EUR_PER_KWH,
    DEFAULT_PRODUCTION_FEE_EUR_PER_KWH,
    DEFAULT_PRODUCTION_VAT,
    DEFAULT_VAT_MODE,
    DEFAULT_VAT_PERCENT,
    VAT_MODE_CUSTOM,
    Kind,
)

# ``(hour_start_utc, spot_eur_per_kwh) -> EUR/kWh``. The hour is needed
# because the VAT rate depends on the date of supply.
Tariff = Callable[[datetime, float], float]

_ESTONIA_TZ = ZoneInfo("Europe/Tallinn")

# Estonian standard VAT rate, keyed by the first local day it applies to.
# Source: Estonian Tax and Customs Board (emta.ee). Days before the first entry
# use ``_ESTONIA_VAT_BEFORE_SCHEDULE``.
ESTONIA_VAT_SCHEDULE: tuple[tuple[date, float], ...] = (
    (date(2009, 7, 1), 20.0),
    (date(2024, 1, 1), 22.0),
    (date(2025, 7, 1), 24.0),
)
_ESTONIA_VAT_BEFORE_SCHEDULE = 18.0


def estonia_vat_percent(at: datetime) -> float:
    """Return the Estonian standard VAT rate for a supply moment.

    The rate changes at local midnight (Europe/Tallinn), so an hour that
    starts at 21:00 UTC on 30 June 2025 (00:00 local, 1 July) is taxed at 24%.
    """
    local_day = at.astimezone(_ESTONIA_TZ).date()
    rate = _ESTONIA_VAT_BEFORE_SCHEDULE
    for since, percent in ESTONIA_VAT_SCHEDULE:
        if local_day < since:
            break
        rate = percent
    return rate


def consumption_price(spot_eur_per_kwh: float, vat_percent: float, margin: float) -> float:
    """Price paid for one kWh bought: ``(spot + margin) * (1 + VAT)``.

    ``margin`` is the seller's margin plus any other per-kWh charges, excluding
    VAT. Negative values are allowed (discounts); negative spot prices are
    passed through because Nord Pool occasionally settles below zero.
    """
    return (spot_eur_per_kwh + margin) * (1 + vat_percent / 100)


def production_price(spot_eur_per_kwh: float, vat_percent: float, fee: float) -> float:
    """Compensation for one kWh sold: ``(spot - fee) * (1 + VAT)``.

    ``vat_percent`` is 0 for sellers who are not VAT-registered, which covers
    most households with a small solar installation.
    """
    return (spot_eur_per_kwh - fee) * (1 + vat_percent / 100)


@dataclass(frozen=True, slots=True)
class PricingConfig:
    """Tariff settings resolved from a config entry's options."""

    vat_mode: str = DEFAULT_VAT_MODE
    vat_percent: float = DEFAULT_VAT_PERCENT
    margin_eur_per_kwh: float = DEFAULT_MARGIN_EUR_PER_KWH
    production_vat: bool = DEFAULT_PRODUCTION_VAT
    production_fee_eur_per_kwh: float = DEFAULT_PRODUCTION_FEE_EUR_PER_KWH

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Self:
        return cls(
            vat_mode=str(options.get(CONF_VAT_MODE, DEFAULT_VAT_MODE)),
            vat_percent=float(options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)),
            margin_eur_per_kwh=float(
                options.get(CONF_MARGIN_EUR_PER_KWH, DEFAULT_MARGIN_EUR_PER_KWH)
            ),
            production_vat=bool(options.get(CONF_PRODUCTION_VAT, DEFAULT_PRODUCTION_VAT)),
            production_fee_eur_per_kwh=float(
                options.get(CONF_PRODUCTION_FEE_EUR_PER_KWH, DEFAULT_PRODUCTION_FEE_EUR_PER_KWH)
            ),
        )

    def vat_at(self, at: datetime) -> float:
        """VAT percentage for a supply moment under the configured mode."""
        if self.vat_mode == VAT_MODE_CUSTOM:
            return self.vat_percent
        return estonia_vat_percent(at)

    def tariff_for(self, kind: Kind) -> Tariff:
        """Per-kWh price function for consumption (cost) or production (compensation)."""
        if kind == Kind.CONSUMPTION:
            margin = self.margin_eur_per_kwh
            return lambda hour, spot: consumption_price(spot, self.vat_at(hour), margin)
        fee = self.production_fee_eur_per_kwh
        if self.production_vat:
            return lambda hour, spot: production_price(spot, self.vat_at(hour), fee)
        return lambda _hour, spot: production_price(spot, 0.0, fee)


def make_tariff(vat_percent: float, margin_eur_per_kwh: float) -> Tariff:
    """Fixed-VAT consumption tariff, mainly for tests and ad-hoc calculations."""
    return lambda _hour, spot: consumption_price(spot, vat_percent, margin_eur_per_kwh)


def compute_cost_rows(
    intervals: list[AccountingInterval],
    kind: Kind,
    prices: dict[datetime, float],
    tariff: Tariff,
    prior_sum: float,
    quarter_prices: Mapping[datetime, float] | None = None,
) -> list[StatisticData]:
    """Build cumulative-sum cost rows from raw intervals and an hourly price map.

    Buckets intervals into hours (matching ``statistics.compute_statistic_rows``),
    multiplies each hour's summed kWh by ``tariff(hour, prices[hour])``, and
    produces a running cumulative-sum series in EUR. Skips hours where the price
    is missing (NPS gap or future hour). Rounds to 4 decimal places (€0.0001).

    With ``quarter_prices`` (15-min intervals), each interval is priced at its
    own quarter's price, as sellers settle since Oct 2025, and the hour's row is
    the sum. A quarter without its own price falls back to the hourly mean.
    """
    if quarter_prices is not None:
        return _quarter_cost_rows(intervals, kind, prices, quarter_prices, tariff, prior_sum)
    hourly: dict[Any, float] = {}
    for ival in intervals:
        value = interval_value(ival, kind)
        if value is None:
            continue
        bucket = ival.period_start.replace(minute=0, second=0, microsecond=0)
        hourly[bucket] = hourly.get(bucket, 0.0) + float(value)

    return compute_cost_rows_from_hourly(hourly, prices, tariff, prior_sum)


def _quarter_cost_rows(
    intervals: list[AccountingInterval],
    kind: Kind,
    prices: Mapping[datetime, float],
    quarter_prices: Mapping[datetime, float],
    tariff: Tariff,
    prior_sum: float,
) -> list[StatisticData]:
    hourly: dict[datetime, float | None] = {}
    for ival in intervals:
        value = interval_value(ival, kind)
        if value is None:
            continue
        hour = ival.period_start.replace(minute=0, second=0, microsecond=0)
        price = quarter_prices.get(ival.period_start, prices.get(hour))
        so_far = hourly.get(hour, 0.0)
        # An hour with any unpriced interval is skipped whole, as in the hourly path.
        if price is None or so_far is None:
            hourly[hour] = None
        else:
            hourly[hour] = so_far + float(value) * tariff(ival.period_start, price)
    rows: list[StatisticData] = []
    running = prior_sum
    for start in sorted(hourly):
        cost = hourly[start]
        if cost is None:
            continue
        running = round(running + round(cost, 4), 4)
        rows.append({"start": start, "state": running, "sum": running})
    return rows


def compute_cost_rows_from_hourly(
    hourly_energy: dict[datetime, float],
    prices: dict[datetime, float],
    tariff: Tariff,
    prior_sum: float,
) -> list[StatisticData]:
    """Build cumulative-sum cost rows from a per-hour energy map.

    ``hourly_energy`` maps top-of-hour UTC to the kWh consumed/produced that
    hour. Each hour's energy is multiplied by ``tariff(hour, prices[hour])`` and
    accumulated into a running EUR series. Hours with no matching price (NPS
    gap or future hour) are skipped. Rounds to 4 decimals (€0.0001).

    Deriving cost from stored hourly energy (rather than re-fetched API
    intervals) keeps the cost statistic exactly consistent with the published
    consumption/production statistics it is meant to price.
    """
    rows: list[StatisticData] = []
    running = prior_sum
    for start in sorted(hourly_energy):
        price = prices.get(start)
        if price is None:
            continue
        cost = round(hourly_energy[start] * tariff(start, price), 4)
        running = round(running + cost, 4)
        rows.append({"start": start, "state": running, "sum": running})
    return rows
