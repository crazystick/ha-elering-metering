"""Pure pricing helpers: VAT schedule, tariff math and cost row construction."""

from __future__ import annotations

import calendar
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
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
    CONF_NETWORK_TARIFFS,
    CONF_PRODUCTION_FEE_EUR_PER_KWH,
    CONF_PRODUCTION_VAT,
    CONF_SUPPLIER_TARIFFS,
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
    """A tariff list could not be parsed or is invalid."""


@dataclass(frozen=True, slots=True)
class SupplierPeriod:
    """The electricity seller's price, valid from ``start`` until the next period.

    Exactly one pricing mode is set: spot + ``spot_margin`` (the default),
    ``fixed_price`` for every hour, or ``fixed_day``/``fixed_night`` split by
    the network day/night hours. All amounts exclude VAT.
    """

    start: date
    spot_margin: float = 0.0
    fixed_price: float | None = None
    fixed_day: float | None = None
    fixed_night: float | None = None
    monthly_fee: float = 0.0

    def unit_price(self, day_hour: bool, spot: float | None) -> float | None:
        """EUR/kWh excl. VAT for one hour, or None when the spot price is missing."""
        if self.fixed_day is not None and self.fixed_night is not None:
            return self.fixed_day if day_hour else self.fixed_night
        if self.fixed_price is not None:
            return self.fixed_price
        return None if spot is None else spot + self.spot_margin


@dataclass(frozen=True, slots=True)
class NetworkPeriod:
    """Network and state charges, valid from ``start`` until the next period.

    ``day``/``night`` apply in the matching hours, ``per_kwh`` in every hour
    (single-rate packages, excise, renewable and supply-security charges).
    All amounts exclude VAT.
    """

    start: date
    day: float = 0.0
    night: float = 0.0
    per_kwh: float = 0.0
    monthly_fee: float = 0.0

    def unit_price(self, day_hour: bool) -> float:
        return (self.day if day_hour else self.night) + self.per_kwh


_SUPPLIER_KEYS = ("spot_margin", "fixed_price", "fixed_day", "fixed_night", "monthly_fee")
_NETWORK_KEYS = ("day", "night", "per_kwh", "monthly_fee")


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


def _parse_periods(text: str | None, keys: tuple[str, ...]) -> list[tuple[date, dict[str, float]]]:
    """Shared YAML parsing: a list of ``{from: date, <amount keys>}`` mappings."""
    if text is None or not text.strip():
        return []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise TariffPeriodError(f"not valid YAML: {err}") from err
    except ValueError as err:
        # PyYAML builds dates while loading, so an impossible unquoted date
        # such as 2026-02-30 raises ValueError rather than YAMLError.
        raise TariffPeriodError(f"invalid value: {err}") from err
    if data is None:
        return []
    if not isinstance(data, list):
        raise TariffPeriodError("expected a list of periods, each starting with '- from:'")

    periods: list[tuple[date, dict[str, float]]] = []
    for index, raw in enumerate(data, start=1):
        if not isinstance(raw, dict):
            raise TariffPeriodError(f"period {index}: expected 'from:' and amounts")
        unknown = set(raw) - {"from", *keys}
        if unknown:
            names = ", ".join(sorted(str(key) for key in unknown))
            raise TariffPeriodError(f"period {index}: unknown key(s) {names}")
        if "from" not in raw:
            raise TariffPeriodError(f"period {index}: missing 'from'")
        amounts: dict[str, float] = {}
        for key in keys:
            if key not in raw:
                continue
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TariffPeriodError(f"period {index}: '{key}' must be a number")
            if not math.isfinite(value):
                raise TariffPeriodError(f"period {index}: '{key}' must be a finite number")
            amounts[key] = float(value)
        if amounts.get("monthly_fee", 0.0) < 0:
            raise TariffPeriodError(f"period {index}: 'monthly_fee' cannot be negative")
        periods.append((_parse_date(raw["from"], index), amounts))

    periods.sort(key=lambda p: p[0])
    for (prev, _), (cur, _) in pairwise(periods):
        if prev == cur:
            raise TariffPeriodError(f"two periods start on {cur.isoformat()}")
    return periods


def parse_supplier_periods(text: str | None) -> tuple[SupplierPeriod, ...]:
    """Parse the supplier tariffs option (YAML list) into sorted periods."""
    result: list[SupplierPeriod] = []
    for index, (start, amounts) in enumerate(_parse_periods(text, _SUPPLIER_KEYS), start=1):
        modes = [
            "spot_margin" in amounts,
            "fixed_price" in amounts,
            "fixed_day" in amounts or "fixed_night" in amounts,
        ]
        if sum(modes) != 1:
            raise TariffPeriodError(
                f"period {index} ({start.isoformat()}): set exactly one of 'spot_margin', "
                "'fixed_price', or 'fixed_day' with 'fixed_night'"
            )
        if ("fixed_day" in amounts) != ("fixed_night" in amounts):
            raise TariffPeriodError(
                f"period {index} ({start.isoformat()}): 'fixed_day' and 'fixed_night' go together"
            )
        result.append(SupplierPeriod(start=start, **amounts))
    return tuple(result)


def parse_network_periods(text: str | None) -> tuple[NetworkPeriod, ...]:
    """Parse the network tariffs option (YAML list) into sorted periods."""
    return tuple(
        NetworkPeriod(start=start, **amounts)
        for start, amounts in _parse_periods(text, _NETWORK_KEYS)
    )


def _period_on[P: (SupplierPeriod, NetworkPeriod)](periods: tuple[P, ...], day: date) -> P | None:
    """The period in force on a local day (latest start on or before it)."""
    current: P | None = None
    for period in periods:
        if period.start > day:
            break
        current = period
    return current


_NO_NETWORK = NetworkPeriod(start=date.min)


def _same_supplier(a: SupplierPeriod, b: SupplierPeriod) -> bool:
    """Same prices, ignoring the start date."""
    return replace(a, start=date.min) == replace(b, start=date.min)


def _same_network(a: NetworkPeriod | None, b: NetworkPeriod | None) -> bool:
    """Same charges, ignoring the start date; no period means no charges."""
    return replace(a or _NO_NETWORK, start=date.min) == replace(b or _NO_NETWORK, start=date.min)


@dataclass(frozen=True, slots=True)
class CostModel:
    """Prices one hour of consumption from supplier and network tariffs, incl. VAT."""

    supplier: tuple[SupplierPeriod, ...]
    network: tuple[NetworkPeriod, ...]
    vat_at: Callable[[datetime], float] = field(compare=False)

    def supplier_on(self, local_day: date) -> SupplierPeriod:
        period = _period_on(self.supplier, local_day)
        return period if period is not None else self.supplier[0]

    def network_on(self, local_day: date) -> NetworkPeriod | None:
        return _period_on(self.network, local_day)

    def hour_cost(self, hour: datetime, energy_kwh: float, spot: float | None) -> float | None:
        local_day = hour.astimezone(_ESTONIA_TZ).date()
        day_hour = is_network_day_hour(hour)
        supplier = self.supplier_on(local_day)
        unit = supplier.unit_price(day_hour, spot)
        if unit is None:
            return None
        fees = supplier.monthly_fee
        network = self.network_on(local_day)
        if network is not None:
            unit += network.unit_price(day_hour)
            fees += network.monthly_fee
        fee = fees / hours_in_local_month(hour) if fees else 0.0
        return (energy_kwh * unit + fee) * (1 + self.vat_at(hour) / 100)


@dataclass(frozen=True, slots=True)
class PricingConfig:
    """Tariff settings resolved from a config entry's options."""

    vat_mode: str = DEFAULT_VAT_MODE
    vat_percent: float = DEFAULT_VAT_PERCENT
    margin_eur_per_kwh: float = DEFAULT_MARGIN_EUR_PER_KWH
    production_vat: bool = DEFAULT_PRODUCTION_VAT
    production_fee_eur_per_kwh: float = DEFAULT_PRODUCTION_FEE_EUR_PER_KWH
    supplier_periods: tuple[SupplierPeriod, ...] = ()
    network_periods: tuple[NetworkPeriod, ...] = ()

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
            supplier_periods=parse_supplier_periods(options.get(CONF_SUPPLIER_TARIFFS)),
            network_periods=parse_network_periods(options.get(CONF_NETWORK_TARIFFS)),
        )

    def vat_at(self, at: datetime) -> float:
        """VAT percentage for a supply moment under the configured mode."""
        if self.vat_mode == VAT_MODE_CUSTOM:
            return self.vat_percent
        return estonia_vat_percent(at)

    def cost_model(self) -> CostModel:
        """Consumption cost model over all time.

        The plain margin applies from the beginning of history; supplier
        periods override it from their start dates. Without network periods
        no network charges are added.
        """
        base = SupplierPeriod(start=date.min, spot_margin=self.margin_eur_per_kwh)
        supplier = (base, *(p for p in self.supplier_periods if p.start > date.min))
        return CostModel(supplier, self.network_periods, self.vat_at)

    def cost_for(self, kind: Kind) -> Tariff | CostModel:
        """Pricer for a cost (consumption) or compensation (production) stream.

        Consumption with supplier or network tariffs returns a ``CostModel``;
        otherwise (and always for production) the per-kWh ``Tariff``.
        """
        if kind == Kind.CONSUMPTION and (self.supplier_periods or self.network_periods):
            return self.cost_model()
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
        new, old = self.cost_model(), previous.cost_model()
        starts = {date.min}
        for model in (new, old):
            starts |= {p.start for p in model.supplier} | {p.start for p in model.network}
        boundaries = sorted(starts)
        for day in boundaries:
            if not _same_supplier(new.supplier_on(day), old.supplier_on(day)):
                return day
            if not _same_network(new.network_on(day), old.network_on(day)):
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
