"""Tests for pricing pure functions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.estfeed.api import AccountingInterval
from custom_components.estfeed.const import (
    CONF_MARGIN_EUR_PER_KWH,
    CONF_PRODUCTION_FEE_EUR_PER_KWH,
    CONF_PRODUCTION_VAT,
    CONF_VAT_MODE,
    CONF_VAT_PERCENT,
    VAT_MODE_CUSTOM,
    VAT_MODE_ESTONIA,
    Kind,
)
from custom_components.estfeed.pricing import (
    PricingConfig,
    compute_cost_rows,
    compute_cost_rows_from_hourly,
    consumption_price,
    estonia_vat_percent,
    make_tariff,
    production_price,
)


def _interval(
    hour: int,
    minute: int = 0,
    consumption: float | None = None,
    production: float | None = None,
) -> AccountingInterval:
    return AccountingInterval(
        period_start=datetime(2026, 5, 21, hour, minute, tzinfo=UTC),
        consumption_kwh=consumption,
        production_kwh=production,
        consumption_m3=None,
        production_m3=None,
    )


_HOUR = datetime(2026, 5, 21, 10, tzinfo=UTC)


def test_consumption_price_vat_only():
    # 0.05 €/kWh * 1.24 = 0.062
    assert consumption_price(0.05, vat_percent=24.0, margin=0.0) == pytest.approx(0.062)


def test_consumption_price_margin_only():
    assert consumption_price(0.05, vat_percent=0.0, margin=0.007) == pytest.approx(0.057)


def test_consumption_price_vat_applies_to_margin():
    # (0.05 + 0.005) * 1.24 = 0.0682 — the margin is entered excluding VAT
    assert consumption_price(0.05, vat_percent=24.0, margin=0.005) == pytest.approx(0.0682)


def test_consumption_price_negative_margin_for_discount():
    assert consumption_price(0.05, vat_percent=0.0, margin=-0.01) == pytest.approx(0.04)


def test_consumption_price_negative_spot():
    # NPS occasionally goes negative; the formula must pass it through.
    assert consumption_price(-0.02, vat_percent=24.0, margin=0.005) == pytest.approx(
        (-0.02 + 0.005) * 1.24
    )


def test_production_price_deducts_fee():
    assert production_price(0.05, vat_percent=0.0, fee=0.003) == pytest.approx(0.047)


def test_production_price_with_vat():
    assert production_price(0.05, vat_percent=24.0, fee=0.003) == pytest.approx(0.047 * 1.24)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2009, 6, 30, 12, tzinfo=UTC), 18.0),
        (datetime(2019, 1, 1, 12, tzinfo=UTC), 20.0),
        # 22:00 UTC on 31 Dec 2023 is already 00:00 on 1 Jan 2024 in Tallinn.
        (datetime(2023, 12, 31, 21, 59, tzinfo=UTC), 20.0),
        (datetime(2023, 12, 31, 22, 0, tzinfo=UTC), 22.0),
        # Summer time: midnight on 1 July 2025 in Tallinn is 21:00 UTC.
        (datetime(2025, 6, 30, 20, 0, tzinfo=UTC), 22.0),
        (datetime(2025, 6, 30, 21, 0, tzinfo=UTC), 24.0),
        (datetime(2026, 10, 5, 12, tzinfo=UTC), 24.0),
    ],
)
def test_estonia_vat_percent_follows_schedule_in_local_time(moment, expected):
    assert estonia_vat_percent(moment) == expected


def test_pricing_config_defaults_use_estonian_schedule_and_untaxed_production():
    config = PricingConfig.from_options({})
    assert config.vat_mode == VAT_MODE_ESTONIA
    cost = config.tariff_for(Kind.CONSUMPTION)
    comp = config.tariff_for(Kind.PRODUCTION)
    assert cost(datetime(2025, 6, 1, tzinfo=UTC), 0.1) == pytest.approx(0.122)
    assert cost(datetime(2025, 8, 1, tzinfo=UTC), 0.1) == pytest.approx(0.124)
    assert comp(datetime(2025, 8, 1, tzinfo=UTC), 0.1) == pytest.approx(0.1)


def test_pricing_config_custom_vat_and_production_settings():
    config = PricingConfig.from_options(
        {
            CONF_VAT_MODE: VAT_MODE_CUSTOM,
            CONF_VAT_PERCENT: 10.0,
            CONF_MARGIN_EUR_PER_KWH: 0.01,
            CONF_PRODUCTION_VAT: True,
            CONF_PRODUCTION_FEE_EUR_PER_KWH: 0.02,
        }
    )
    assert config.tariff_for(Kind.CONSUMPTION)(_HOUR, 0.1) == pytest.approx(0.121)
    assert config.tariff_for(Kind.PRODUCTION)(_HOUR, 0.1) == pytest.approx(0.088)


def test_make_tariff_returns_fixed_vat_consumption_tariff():
    tariff = make_tariff(vat_percent=24.0, margin_eur_per_kwh=0.005)
    assert tariff(_HOUR, 0.05) == pytest.approx(0.0682)
    assert make_tariff(22.0, 0.0)(_HOUR, 0.05) != make_tariff(24.0, 0.0)(_HOUR, 0.05)


def test_compute_cost_rows_uses_vat_of_each_hour():
    # Two hours straddling the 22% → 24% change at local midnight.
    before = datetime(2025, 6, 30, 20, tzinfo=UTC)
    after = datetime(2025, 6, 30, 21, tzinfo=UTC)
    tariff = PricingConfig().tariff_for(Kind.CONSUMPTION)
    rows = compute_cost_rows_from_hourly(
        {before: 1.0, after: 1.0}, {before: 0.1, after: 0.1}, tariff, prior_sum=0.0
    )
    assert [r["sum"] for r in rows] == [pytest.approx(0.122), pytest.approx(0.246)]


def test_compute_cost_rows_single_hour():
    intervals = [_interval(10, consumption=2.0)]
    prices = {datetime(2026, 5, 21, 10, tzinfo=UTC): 0.05}
    tariff = make_tariff(vat_percent=0.0, margin_eur_per_kwh=0.0)
    rows = compute_cost_rows(intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=0.0)
    assert len(rows) == 1
    assert rows[0]["start"] == datetime(2026, 5, 21, 10, tzinfo=UTC)
    # 2.0 kWh * 0.05 €/kWh = 0.10 €
    assert rows[0]["sum"] == pytest.approx(0.10)
    assert rows[0]["state"] == rows[0]["sum"]


def test_compute_cost_rows_aggregates_quarter_hour_intervals():
    intervals = [
        _interval(10, 0, consumption=0.1),
        _interval(10, 15, consumption=0.2),
        _interval(10, 30, consumption=0.3),
        _interval(10, 45, consumption=0.4),
    ]
    prices = {datetime(2026, 5, 21, 10, tzinfo=UTC): 0.05}
    tariff = make_tariff(0.0, 0.0)
    rows = compute_cost_rows(intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=0.0)
    assert len(rows) == 1
    # 1.0 kWh * 0.05 = 0.05
    assert rows[0]["sum"] == pytest.approx(0.05)


def _q(minute: int) -> datetime:
    return datetime(2026, 5, 21, 10, minute, tzinfo=UTC)


def test_compute_cost_rows_prices_each_quarter_on_its_own():
    # All use falls in the cheapest quarter: the hourly mean would overcharge it.
    intervals = [_interval(10, 0, consumption=2.0), _interval(10, 45, consumption=0.0)]
    prices = {_q(0): 0.10}  # hourly mean of the quarters below
    quarters = {_q(0): 0.02, _q(15): 0.10, _q(30): 0.10, _q(45): 0.18}
    rows = compute_cost_rows(
        intervals,
        Kind.CONSUMPTION,
        prices,
        make_tariff(0.0, 0.0),
        prior_sum=1.0,
        quarter_prices=quarters,
    )
    # 1.0 prior + 2.0 kWh * 0.02 = 1.04, not 1.0 + 2.0 * 0.10 = 1.20 at the hourly mean
    assert [r["start"] for r in rows] == [_q(0)]
    assert rows[0]["sum"] == pytest.approx(1.04)
    assert rows[0]["state"] == rows[0]["sum"]


def test_compute_cost_rows_quarter_falls_back_to_hourly_price():
    intervals = [_interval(10, 0, consumption=1.0), _interval(10, 15, consumption=1.0)]
    prices = {_q(0): 0.05}
    quarters = {_q(0): 0.03}
    rows = compute_cost_rows(
        intervals,
        Kind.CONSUMPTION,
        prices,
        make_tariff(24.0, 0.0),
        prior_sum=0.0,
        quarter_prices=quarters,
    )
    # (1.0 * 0.03 + 1.0 * 0.05) * 1.24 = 0.0992
    assert rows[0]["sum"] == pytest.approx(0.0992)


def test_compute_cost_rows_quarter_skips_hour_with_unpriced_interval():
    intervals = [
        _interval(10, 0, consumption=1.0),
        _interval(11, 0, consumption=1.0),
        _interval(11, 15, consumption=1.0),
    ]
    eleven = datetime(2026, 5, 21, 11, tzinfo=UTC)
    rows = compute_cost_rows(
        intervals,
        Kind.CONSUMPTION,
        prices={_q(0): 0.05},
        tariff=make_tariff(0.0, 0.0),
        prior_sum=0.0,
        quarter_prices={_q(0): 0.05, eleven: 0.07},
    )
    # Hour 11 has no price for :15 and no hourly price: skipped whole, like the hourly path.
    assert [r["start"] for r in rows] == [_q(0)]
    assert rows[0]["sum"] == pytest.approx(0.05)


def test_compute_cost_rows_empty_quarter_map_matches_hourly_path():
    intervals = [_interval(10, 0, consumption=0.4), _interval(10, 30, consumption=0.6)]
    prices = {_q(0): 0.05}
    tariff = make_tariff(24.0, 0.005)
    hourly = compute_cost_rows(intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=0.0)
    quarter = compute_cost_rows(
        intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=0.0, quarter_prices={}
    )
    assert quarter == hourly


def test_compute_cost_rows_applies_tariff():
    intervals = [_interval(10, consumption=2.0)]
    prices = {datetime(2026, 5, 21, 10, tzinfo=UTC): 0.05}
    tariff = make_tariff(vat_percent=24.0, margin_eur_per_kwh=0.01)
    rows = compute_cost_rows(intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=0.0)
    # tariff(0.05) = (0.05 + 0.01) * 1.24 = 0.0744; * 2.0 kWh = 0.1488
    assert rows[0]["sum"] == pytest.approx(0.1488)


def test_compute_cost_rows_skips_missing_price_hours():
    intervals = [
        _interval(10, consumption=1.0),
        _interval(11, consumption=1.0),
        _interval(12, consumption=1.0),
    ]
    # Only 10:00 and 12:00 have prices; 11:00 is missing.
    prices = {
        datetime(2026, 5, 21, 10, tzinfo=UTC): 0.05,
        datetime(2026, 5, 21, 12, tzinfo=UTC): 0.06,
    }
    tariff = make_tariff(0.0, 0.0)
    rows = compute_cost_rows(intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=0.0)
    assert len(rows) == 2
    starts = [r["start"] for r in rows]
    assert datetime(2026, 5, 21, 11, tzinfo=UTC) not in starts
    # Cumulative continues across the gap: 0.05 then 0.05+0.06=0.11
    assert rows[0]["sum"] == pytest.approx(0.05)
    assert rows[1]["sum"] == pytest.approx(0.11)


def test_compute_cost_rows_skips_none_values():
    intervals = [
        _interval(10, consumption=None),
        _interval(11, consumption=2.0),
    ]
    prices = {
        datetime(2026, 5, 21, 10, tzinfo=UTC): 0.05,
        datetime(2026, 5, 21, 11, tzinfo=UTC): 0.05,
    }
    tariff = make_tariff(0.0, 0.0)
    rows = compute_cost_rows(intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=0.0)
    assert len(rows) == 1
    assert rows[0]["start"] == datetime(2026, 5, 21, 11, tzinfo=UTC)
    assert rows[0]["sum"] == pytest.approx(0.10)


def test_compute_cost_rows_carries_prior_sum():
    intervals = [_interval(10, consumption=2.0)]
    prices = {datetime(2026, 5, 21, 10, tzinfo=UTC): 0.05}
    tariff = make_tariff(0.0, 0.0)
    rows = compute_cost_rows(intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=100.0)
    # 100.0 + 0.10
    assert rows[0]["sum"] == pytest.approx(100.10)


def test_compute_cost_rows_production_kind():
    intervals = [_interval(10, production=1.5)]
    prices = {datetime(2026, 5, 21, 10, tzinfo=UTC): 0.04}
    tariff = make_tariff(0.0, 0.0)
    rows = compute_cost_rows(intervals, Kind.PRODUCTION, prices, tariff, prior_sum=0.0)
    assert rows[0]["sum"] == pytest.approx(0.06)


def test_compute_cost_rows_sorts_by_start():
    intervals = [
        _interval(12, consumption=1.0),
        _interval(10, consumption=1.0),
        _interval(11, consumption=1.0),
    ]
    prices = {datetime(2026, 5, 21, h, tzinfo=UTC): 0.05 for h in (10, 11, 12)}
    tariff = make_tariff(0.0, 0.0)
    rows = compute_cost_rows(intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=0.0)
    starts = [r["start"] for r in rows]
    assert starts == sorted(starts)


def test_compute_cost_rows_rounds_to_four_decimals():
    # 0.333333 kWh * 0.05 = 0.0166666...; rounds to 0.0167
    intervals = [_interval(10, consumption=0.333333)]
    prices = {datetime(2026, 5, 21, 10, tzinfo=UTC): 0.05}
    tariff = make_tariff(0.0, 0.0)
    rows = compute_cost_rows(intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=0.0)
    assert rows[0]["sum"] == pytest.approx(0.0167)


def test_compute_cost_rows_from_hourly_basic():
    # Two hours of energy, flat 0.05 €/kWh, no VAT/margin → cumulative cost.
    hourly = {
        datetime(2026, 5, 21, 10, tzinfo=UTC): 2.0,
        datetime(2026, 5, 21, 11, tzinfo=UTC): 3.0,
    }
    prices = {
        datetime(2026, 5, 21, 10, tzinfo=UTC): 0.05,
        datetime(2026, 5, 21, 11, tzinfo=UTC): 0.05,
    }
    tariff = make_tariff(0.0, 0.0)
    rows = compute_cost_rows_from_hourly(hourly, prices, tariff, prior_sum=0.0)
    assert [r["sum"] for r in rows] == [pytest.approx(0.1), pytest.approx(0.25)]


def test_compute_cost_rows_from_hourly_skips_missing_price():
    hourly = {
        datetime(2026, 5, 21, 10, tzinfo=UTC): 2.0,
        datetime(2026, 5, 21, 11, tzinfo=UTC): 3.0,
    }
    # Only hour 11 has a price.
    prices = {datetime(2026, 5, 21, 11, tzinfo=UTC): 0.05}
    tariff = make_tariff(0.0, 0.0)
    rows = compute_cost_rows_from_hourly(hourly, prices, tariff, prior_sum=0.0)
    assert len(rows) == 1
    assert rows[0]["start"] == datetime(2026, 5, 21, 11, tzinfo=UTC)
    assert rows[0]["sum"] == pytest.approx(0.15)


def test_compute_cost_rows_delegates_to_hourly_builder():
    # compute_cost_rows must produce identical output to first bucketing
    # intervals then calling the hourly builder.
    intervals = [
        _interval(10, 0, consumption=0.5),
        _interval(10, 15, consumption=0.5),
        _interval(11, 0, consumption=1.0),
    ]
    prices = {datetime(2026, 5, 21, h, tzinfo=UTC): 0.05 for h in (10, 11)}
    tariff = make_tariff(22.0, 0.0)
    via_intervals = compute_cost_rows(intervals, Kind.CONSUMPTION, prices, tariff, prior_sum=0.0)
    via_hourly = compute_cost_rows_from_hourly(
        {
            datetime(2026, 5, 21, 10, tzinfo=UTC): 1.0,
            datetime(2026, 5, 21, 11, tzinfo=UTC): 1.0,
        },
        prices,
        tariff,
        prior_sum=0.0,
    )
    assert via_intervals == via_hourly
