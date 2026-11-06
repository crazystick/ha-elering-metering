"""Pure pricing helpers: VAT schedule, tariff math and cost row construction."""

from __future__ import annotations

import calendar
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from itertools import pairwise
from typing import Any, Self
from zoneinfo import ZoneInfo

import yaml
from homeassistant.components.recorder.models import StatisticData

from .api import AccountingInterval, interval_value
from .const import (
    CONF_MARGIN_EUR_PER_KWH,
    CONF_PRODUCTION_FEE_EUR_PER_KWH,
    CONF_PRODUCTION_VAT,
    CONF_TARIFF_PERIODS,
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


# ---------------------------------------------------------------------------
# Estonian public holidays and the network day/night split
# ---------------------------------------------------------------------------

# Network operators (Elektrilevi and others) bill the "day" rate Monday to
# Friday 07:00-22:00 local time, except on public holidays, and the "night"
# rate at all other times. The split does not move with daylight saving.
DAY_RATE_START_HOUR = 7
DAY_RATE_END_HOUR = 22


def _easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=64)
def estonia_public_holidays(year: int) -> frozenset[date]:
    """Estonian public holidays (riigipühad) for ``year``.

    Source: Public Holidays and Days of National Importance Act (riigi teataja).
    Easter Sunday and Pentecost always fall on a Sunday, but are listed for
    completeness.
    """
    easter = _easter_sunday(year)
    return frozenset(
        {
            date(year, 1, 1),  # New Year's Day
            date(year, 2, 24),  # Independence Day
            easter - timedelta(days=2),  # Good Friday
            easter,  # Easter Sunday
            date(year, 5, 1),  # Spring Day
            easter + timedelta(days=49),  # Pentecost
            date(year, 6, 23),  # Victory Day
            date(year, 6, 24),  # Midsummer Day
            date(year, 8, 20),  # Day of Restoration of Independence
            date(year, 12, 24),  # Christmas Eve
            date(year, 12, 25),  # Christmas Day
            date(year, 12, 26),  # Boxing Day
        }
    )


def is_network_day_hour(hour: datetime) -> bool:
    """True when ``hour`` (any tz) falls in the network "day" rate window."""
    local = hour.astimezone(_ESTONIA_TZ)
    if local.weekday() >= 5 or local.date() in estonia_public_holidays(local.year):
        return False
    return DAY_RATE_START_HOUR <= local.hour < DAY_RATE_END_HOUR


def local_midnight(day: date) -> datetime:
    """Start of an Estonian calendar day, as an aware datetime."""
    return datetime(day.year, day.month, day.day, tzinfo=_ESTONIA_TZ)


def hours_in_local_month(hour: datetime) -> int:
    """Number of real hours in the Estonian calendar month containing ``hour``.

    743 or 745 in the months where daylight saving starts or ends.
    """
    local = hour.astimezone(_ESTONIA_TZ)
    first = datetime(local.year, local.month, 1, tzinfo=_ESTONIA_TZ)
    days = calendar.monthrange(local.year, local.month)[1]
    nxt = first + timedelta(days=days)
    nxt = datetime(nxt.year, nxt.month, 1, tzinfo=_ESTONIA_TZ)
    return round((nxt.astimezone(UTC) - first.astimezone(UTC)).total_seconds() / 3600)


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


class TariffPeriodError(ValueError):
    """The tariff periods text could not be parsed or is invalid."""


@dataclass(frozen=True, slots=True)
class TariffPeriod:
    """One consumption tariff, valid from ``start`` until the next period.

    All amounts exclude VAT. ``fixed_price`` set means a fixed-price contract
    (the spot price is ignored); otherwise the price is spot + ``spot_margin``.
    ``day``/``night`` are per-kWh network charges added in the matching
    hours; ``monthly_fee`` is spread evenly over the hours of each month.
    """

    start: date
    spot_margin: float = 0.0
    fixed_price: float | None = None
    day: float = 0.0
    night: float = 0.0
    monthly_fee: float = 0.0

    def same_tariff(self, other: TariffPeriod) -> bool:
        """Equal apart from the start date."""
        return (
            self.spot_margin,
            self.fixed_price,
            self.day,
            self.night,
            self.monthly_fee,
        ) == (other.spot_margin, other.fixed_price, other.day, other.night, other.monthly_fee)


_PERIOD_AMOUNT_KEYS = ("spot_margin", "fixed_price", "day", "night", "monthly_fee")


def _parse_date(value: Any, index: int) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise TariffPeriodError(f"period {index}: 'from' must be a date like 2026-12-01")


def parse_tariff_periods(text: str | None) -> tuple[TariffPeriod, ...]:
    """Parse the YAML tariff-periods option into sorted periods.

    Empty text means "no periods" (the plain margin applies throughout).
    Raises ``TariffPeriodError`` with a user-facing message on bad input.
    """
    if text is None or not text.strip():
        return ()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise TariffPeriodError(f"not valid YAML: {err}") from err
    if data is None:
        return ()
    if not isinstance(data, list):
        raise TariffPeriodError("expected a list of periods, each starting with '- from:'")

    periods: list[TariffPeriod] = []
    for index, raw in enumerate(data, start=1):
        if not isinstance(raw, dict):
            raise TariffPeriodError(f"period {index}: expected 'from:' and amounts")
        unknown = set(raw) - {"from", *_PERIOD_AMOUNT_KEYS}
        if unknown:
            raise TariffPeriodError(f"period {index}: unknown key(s) {', '.join(sorted(unknown))}")
        if "from" not in raw:
            raise TariffPeriodError(f"period {index}: missing 'from'")
        if ("spot_margin" in raw) == ("fixed_price" in raw):
            raise TariffPeriodError(
                f"period {index}: set exactly one of 'spot_margin' or 'fixed_price'"
            )
        amounts: dict[str, float] = {}
        for key in _PERIOD_AMOUNT_KEYS:
            if key not in raw:
                continue
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TariffPeriodError(f"period {index}: '{key}' must be a number")
            amounts[key] = float(value)
        if amounts.get("monthly_fee", 0.0) < 0:
            raise TariffPeriodError(f"period {index}: 'monthly_fee' cannot be negative")
        periods.append(TariffPeriod(start=_parse_date(raw["from"], index), **amounts))

    periods.sort(key=lambda p: p.start)
    for prev, cur in pairwise(periods):
        if prev.start == cur.start:
            raise TariffPeriodError(f"two periods start on {cur.start.isoformat()}")
    return tuple(periods)


@dataclass(frozen=True, slots=True)
class CostModel:
    """Prices one hour of consumption under dated tariff periods, incl. VAT."""

    periods: tuple[TariffPeriod, ...]
    vat_at: Callable[[datetime], float] = field(compare=False)

    def period_at(self, hour: datetime) -> TariffPeriod:
        return self.period_on(hour.astimezone(_ESTONIA_TZ).date())

    def period_on(self, local_day: date) -> TariffPeriod:
        current = self.periods[0]
        for period in self.periods:
            if period.start > local_day:
                break
            current = period
        return current

    def hour_cost(self, hour: datetime, energy_kwh: float, spot: float | None) -> float | None:
        period = self.period_at(hour)
        if period.fixed_price is not None:
            unit = period.fixed_price
        elif spot is None:
            return None
        else:
            unit = spot + period.spot_margin
        unit += period.day if is_network_day_hour(hour) else period.night
        fee = period.monthly_fee / hours_in_local_month(hour) if period.monthly_fee else 0.0
        return (energy_kwh * unit + fee) * (1 + self.vat_at(hour) / 100)


@dataclass(frozen=True, slots=True)
class PricingConfig:
    """Tariff settings resolved from a config entry's options."""

    vat_mode: str = DEFAULT_VAT_MODE
    vat_percent: float = DEFAULT_VAT_PERCENT
    margin_eur_per_kwh: float = DEFAULT_MARGIN_EUR_PER_KWH
    production_vat: bool = DEFAULT_PRODUCTION_VAT
    production_fee_eur_per_kwh: float = DEFAULT_PRODUCTION_FEE_EUR_PER_KWH
    tariff_periods: tuple[TariffPeriod, ...] = ()

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
            tariff_periods=parse_tariff_periods(options.get(CONF_TARIFF_PERIODS)),
        )

    def vat_at(self, at: datetime) -> float:
        """VAT percentage for a supply moment under the configured mode."""
        if self.vat_mode == VAT_MODE_CUSTOM:
            return self.vat_percent
        return estonia_vat_percent(at)

    def effective_periods(self) -> tuple[TariffPeriod, ...]:
        """Consumption tariffs over all time.

        The plain margin applies from the beginning of history; configured
        periods override it from their start dates.
        """
        base = TariffPeriod(start=date.min, spot_margin=self.margin_eur_per_kwh)
        return (base, *(p for p in self.tariff_periods if p.start > date.min))

    def cost_for(self, kind: Kind) -> Tariff | CostModel:
        """Pricer for a cost (consumption) or compensation (production) stream.

        Consumption with tariff periods returns a ``CostModel`` (fixed prices,
        day/night charges, monthly fees); otherwise the per-kWh ``Tariff``.
        """
        if kind == Kind.CONSUMPTION and self.tariff_periods:
            return CostModel(self.effective_periods(), self.vat_at)
        return self.tariff_for(kind)

    def rebuild_from(self, previous: PricingConfig) -> date | None:
        """First local day whose cost differs from ``previous``'s settings.

        ``None`` means nothing changed. ``date.min`` means everything must be
        recomputed (VAT or production settings changed, or the change reaches
        back before the first period).
        """
        if (
            self.vat_mode,
            self.vat_percent,
            self.production_vat,
            self.production_fee_eur_per_kwh,
        ) != (
            previous.vat_mode,
            previous.vat_percent,
            previous.production_vat,
            previous.production_fee_eur_per_kwh,
        ):
            return date.min
        new, old = (
            CostModel(self.effective_periods(), self.vat_at),
            CostModel(previous.effective_periods(), previous.vat_at),
        )
        boundaries = sorted({p.start for p in (*new.periods, *old.periods)})
        for day in boundaries:
            if not new.period_on(day).same_tariff(old.period_on(day)):
                return day
        return None

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
    tariff: Tariff | CostModel,
    prior_sum: float,
) -> list[StatisticData]:
    """Build cumulative-sum cost rows from raw intervals and an hourly price map.

    Buckets intervals into hours (matching ``statistics.compute_statistic_rows``),
    multiplies each hour's summed kWh by ``tariff(hour, prices[hour])``, and
    produces a running cumulative-sum series in EUR. Skips hours where the price
    is missing (NPS gap or future hour). Rounds to 4 decimal places (€0.0001).
    """
    hourly: dict[Any, float] = {}
    for ival in intervals:
        value = interval_value(ival, kind)
        if value is None:
            continue
        bucket = ival.period_start.replace(minute=0, second=0, microsecond=0)
        hourly[bucket] = hourly.get(bucket, 0.0) + float(value)

    return compute_cost_rows_from_hourly(hourly, prices, tariff, prior_sum)


def compute_cost_rows_from_hourly(
    hourly_energy: dict[datetime, float],
    prices: dict[datetime, float],
    tariff: Tariff | CostModel,
    prior_sum: float,
) -> list[StatisticData]:
    """Build cumulative-sum cost rows from a per-hour energy map.

    ``hourly_energy`` maps top-of-hour UTC to the kWh consumed/produced that
    hour. Each hour's energy is multiplied by ``tariff(hour, prices[hour])`` and
    accumulated into a running EUR series. Hours with no matching price (NPS
    gap or future hour) are skipped. A ``CostModel`` prices whole hours instead
    (fixed prices, day/night network charges, monthly fees) and can price
    fixed-price hours without a spot price. Rounds to 4 decimals (€0.0001).

    Deriving cost from stored hourly energy (rather than re-fetched API
    intervals) keeps the cost statistic exactly consistent with the published
    consumption/production statistics it is meant to price.
    """
    rows: list[StatisticData] = []
    running = prior_sum
    for start in sorted(hourly_energy):
        price = prices.get(start)
        if isinstance(tariff, CostModel):
            hour_cost = tariff.hour_cost(start, hourly_energy[start], price)
        elif price is None:
            hour_cost = None
        else:
            hour_cost = hourly_energy[start] * tariff(start, price)
        if hour_cost is None:
            continue
        cost = round(hour_cost, 4)
        running = round(running + cost, 4)
        rows.append({"start": start, "state": running, "sum": running})
    return rows
