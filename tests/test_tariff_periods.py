"""Tests for dated tariff periods, fixed prices, day/night network charges and monthly fees."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.estfeed.const import (
    CONF_MARGIN_EUR_PER_KWH,
    CONF_TARIFF_PERIODS,
    CONF_VAT_MODE,
    CONF_VAT_PERCENT,
    VAT_MODE_CUSTOM,
    Kind,
)
from custom_components.estfeed.pricing import (
    CostModel,
    PricingConfig,
    TariffPeriod,
    TariffPeriodError,
    compute_cost_rows_from_hourly,
    estonia_public_holidays,
    hours_in_local_month,
    is_network_day_hour,
    local_midnight,
    parse_tariff_periods,
)

TALLINN = ZoneInfo("Europe/Tallinn")


def _local(*args: int) -> datetime:
    return datetime(*args, tzinfo=TALLINN).astimezone(UTC)


def _vat24(_hour: datetime) -> float:
    return 24.0


# ---- holidays and the day/night split ----


@pytest.mark.parametrize(
    ("year", "good_friday", "pentecost"),
    [
        (2025, date(2025, 4, 18), date(2025, 6, 8)),
        (2026, date(2026, 4, 3), date(2026, 5, 24)),
        (2027, date(2027, 3, 26), date(2027, 5, 16)),
    ],
)
def test_estonian_holidays_include_moveable_feasts(year, good_friday, pentecost):
    holidays = estonia_public_holidays(year)
    assert good_friday in holidays
    assert pentecost in holidays
    assert len(holidays) == 12
    assert {date(year, 2, 24), date(year, 6, 23), date(year, 8, 20), date(year, 12, 26)} <= holidays


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        ((2026, 11, 6, 6), False),  # Friday before 07:00
        ((2026, 11, 6, 7), True),  # Friday 07:00
        ((2026, 11, 6, 21), True),  # Friday 21:00
        ((2026, 11, 6, 22), False),  # Friday 22:00
        ((2026, 11, 7, 12), False),  # Saturday
        ((2026, 11, 8, 12), False),  # Sunday
        ((2026, 12, 24, 12), False),  # Thursday, Christmas Eve
        ((2026, 4, 3, 12), False),  # Good Friday
        ((2026, 6, 1, 7), True),  # Monday in summer time: still 07:00 local
        ((2026, 6, 1, 6), False),
    ],
)
def test_network_day_hour(local, expected):
    assert is_network_day_hour(_local(*local)) is expected


@pytest.mark.parametrize(
    ("month", "hours"),
    [(11, 720), (2, 672), (3, 743), (10, 745), (1, 744)],
)
def test_hours_in_local_month_handles_dst(month, hours):
    assert hours_in_local_month(_local(2026, month, 15, 12)) == hours


def test_local_month_uses_estonian_calendar():
    # 23:30 UTC on 31 Oct is already 1 Nov in Tallinn.
    assert hours_in_local_month(datetime(2026, 10, 31, 23, tzinfo=UTC)) == 720


# ---- parsing ----


_EXAMPLE = """
- from: 2026-12-01
  fixed_price: 0.149
  day: 0.0369
  night: 0.021
  monthly_fee: 20.64
- from: "2025-07-01"   # quoted dates work too
  spot_margin: 0.0207
"""


def test_parse_sorts_and_reads_all_fields():
    periods = parse_tariff_periods(_EXAMPLE)
    assert [p.start for p in periods] == [date(2025, 7, 1), date(2026, 12, 1)]
    assert periods[0] == TariffPeriod(start=date(2025, 7, 1), spot_margin=0.0207)
    assert periods[1] == TariffPeriod(
        start=date(2026, 12, 1), fixed_price=0.149, day=0.0369, night=0.021, monthly_fee=20.64
    )


@pytest.mark.parametrize("text", [None, "", "   \n", "# only a comment\n"])
def test_parse_empty_means_no_periods(text):
    assert parse_tariff_periods(text) == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("from: 2026-12-01", "expected a list"),
        ("- 0.1", "expected 'from:'"),
        ("- spot_margin: 0.1", "missing 'from'"),
        ("- from: 2026-12-01", "exactly one"),
        ("- from: 2026-12-01\n  spot_margin: 0.1\n  fixed_price: 0.2", "exactly one"),
        ("- from: 2026-12-01\n  fixed_price: cheap", "must be a number"),
        ("- from: 2026-12-01\n  fixed_price: true", "must be a number"),
        ("- from: 2026-12-01\n  fixed: 0.1", "unknown key"),
        ("- from: next week\n  fixed_price: 0.1", "must be a date"),
        ("- from: 2026-12-01\n  fixed_price: 0.1\n  monthly_fee: -1", "cannot be negative"),
        (
            "- from: 2026-12-01\n  fixed_price: 0.1\n- from: 2026-12-01\n  fixed_price: 0.2",
            "two periods start on 2026-12-01",
        ),
        ("- from: [", "not valid YAML"),
    ],
)
def test_parse_rejects_bad_input(text, message):
    with pytest.raises(TariffPeriodError, match=message):
        parse_tariff_periods(text)


# ---- pricing one hour ----


def _model(*periods: TariffPeriod) -> CostModel:
    return CostModel((TariffPeriod(start=date.min, spot_margin=0.01), *periods), _vat24)


_WINTER = TariffPeriod(
    start=date(2026, 12, 1), fixed_price=0.10, day=0.04, night=0.02, monthly_fee=31.0
)


def test_fixed_price_ignores_spot_and_adds_day_rate_and_fee():
    hour = _local(2026, 12, 2, 10)  # Wednesday, day rate; December has 744 h
    expected = (2.0 * (0.10 + 0.04) + 31.0 / 744) * 1.24
    model = _model(_WINTER)
    assert model.hour_cost(hour, 2.0, 5.0) == pytest.approx(expected)
    assert model.hour_cost(hour, 2.0, None) == pytest.approx(expected)


def test_night_rate_applies_at_weekend():
    hour = _local(2026, 12, 5, 10)  # Saturday
    expected = (1.0 * (0.10 + 0.02) + 31.0 / 744) * 1.24
    assert _model(_WINTER).hour_cost(hour, 1.0, None) == pytest.approx(expected)


def test_spot_period_needs_a_spot_price():
    hour = _local(2026, 11, 2, 10)  # before the winter period → base spot tariff
    model = _model(_WINTER)
    assert model.hour_cost(hour, 2.0, None) is None
    assert model.hour_cost(hour, 2.0, 0.05) == pytest.approx(2.0 * 0.06 * 1.24)


def test_period_switches_at_local_midnight():
    model = _model(_WINTER)
    # 21:59 UTC on 30 Nov is 23:59 local → still November (spot).
    assert model.period_at(datetime(2026, 11, 30, 21, 59, tzinfo=UTC)).fixed_price is None
    # 22:00 UTC on 30 Nov is 00:00 local on 1 Dec → winter period.
    assert model.period_at(datetime(2026, 11, 30, 22, tzinfo=UTC)) == _WINTER


def test_monthly_fee_adds_up_to_the_fee_over_a_month():
    model = _model(_WINTER)
    start = local_midnight(date(2026, 12, 1)).astimezone(UTC)
    hours = [start + timedelta(hours=i) for i in range(744)]
    total = sum(model.hour_cost(h, 0.0, None) or 0.0 for h in hours)
    assert total == pytest.approx(31.0 * 1.24)


def test_compute_rows_prices_fixed_hours_without_spot_prices():
    model = _model(_WINTER)
    h1, h2 = _local(2026, 11, 30, 23), _local(2026, 12, 1, 0)  # spot hour, fixed hour
    rows = compute_cost_rows_from_hourly({h1: 1.0, h2: 1.0}, {}, model, prior_sum=10.0)
    # The spot hour has no price → skipped; the fixed hour is priced.
    assert [r["start"] for r in rows] == [h2]
    assert rows[0]["sum"] == pytest.approx(10.0 + round((0.12 + 31.0 / 744) * 1.24, 4))


# ---- PricingConfig integration ----


def _config(periods: str = "", margin: float = 0.01, vat: float = 24.0) -> PricingConfig:
    return PricingConfig.from_options(
        {
            CONF_VAT_MODE: VAT_MODE_CUSTOM,
            CONF_VAT_PERCENT: vat,
            CONF_MARGIN_EUR_PER_KWH: margin,
            CONF_TARIFF_PERIODS: periods,
        }
    )


_WINTER_YAML = "- from: 2026-12-01\n  fixed_price: 0.10\n  monthly_fee: 31\n"


def test_without_periods_cost_is_the_plain_tariff():
    config = _config()
    pricer = config.cost_for(Kind.CONSUMPTION)
    assert not isinstance(pricer, CostModel)
    assert pricer(_local(2026, 12, 2, 10), 0.05) == pytest.approx(0.06 * 1.24)


def test_margin_applies_before_the_first_period():
    pricer = _config(_WINTER_YAML).cost_for(Kind.CONSUMPTION)
    assert isinstance(pricer, CostModel)
    assert pricer.hour_cost(_local(2026, 11, 2, 10), 1.0, 0.05) == pytest.approx(0.06 * 1.24)
    assert pricer.hour_cost(_local(2026, 12, 2, 10), 1.0, 0.05) == pytest.approx(
        (0.10 + 31.0 / 744) * 1.24
    )


def test_periods_do_not_change_production_compensation():
    with_periods = _config(_WINTER_YAML).cost_for(Kind.PRODUCTION)
    without = _config().cost_for(Kind.PRODUCTION)
    hour = _local(2026, 12, 2, 10)
    assert not isinstance(with_periods, CostModel)
    assert not isinstance(without, CostModel)
    assert with_periods(hour, 0.05) == without(hour, 0.05)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (_config(), _config(), None),
        (_config(), _config(_WINTER_YAML), date(2026, 12, 1)),
        (_config(_WINTER_YAML), _config(), date(2026, 12, 1)),
        (_config(_WINTER_YAML), _config(_WINTER_YAML + "  day: 0.04\n"), date(2026, 12, 1)),
        (
            _config(_WINTER_YAML),
            _config(_WINTER_YAML + "- from: 2027-04-01\n  spot_margin: 0.01\n"),
            date(2027, 4, 1),
        ),
        # Comments or reordering only: nothing to recompute.
        (_config(_WINTER_YAML), _config("# winter\n" + _WINTER_YAML), None),
        # A period that repeats the base tariff changes nothing either.
        (_config(), _config("- from: 2026-12-01\n  spot_margin: 0.01\n"), None),
        (_config(margin=0.01), _config(margin=0.02), date.min),
        (_config(vat=24.0), _config(vat=22.0), date.min),
    ],
)
def test_rebuild_from_finds_first_changed_day(old, new, expected):
    assert new.rebuild_from(old) == expected
