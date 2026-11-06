"""Tests for entry setup and service targeting."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.estfeed import (
    _async_options_updated,
    _async_register_services,
    async_migrate_entry,
    async_setup_entry,
)
from custom_components.estfeed.api import EstfeedAuthError
from custom_components.estfeed.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_MARGIN_EUR_PER_KWH,
    CONF_PRODUCTION_FEE_EUR_PER_KWH,
    CONF_PRODUCTION_VAT,
    CONF_SUPPLIER_TARIFFS,
    CONF_VAT_MODE,
    CONF_VAT_PERCENT,
    DOMAIN,
    VAT_MODE_CUSTOM,
    Kind,
)
from custom_components.estfeed.pricing import PricingConfig


@pytest.mark.parametrize("service", ["backfill_history", "set_cumulative_reset_at"])
@pytest.mark.parametrize("target", [None, "first", "missing", ""])
async def test_service_targets_only_selected_loaded_entries(hass, service, target):
    first, second = MagicMock(), MagicMock()
    for coordinator in (first, second):
        coordinator.async_initial_backfill = AsyncMock()
        coordinator.async_set_cumulative_reset_at = AsyncMock()
    hass.data[DOMAIN] = {"first": first, "second": second}
    _async_register_services(hass)
    data = {"reset_at": "2026-05-18T12:00:00+00:00"} if service == "set_cumulative_reset_at" else {}
    if target is not None:
        data["entry_id"] = target
    if target in ("missing", ""):
        with pytest.raises(ServiceValidationError, match="not loaded"):
            await hass.services.async_call(DOMAIN, service, data, blocking=True)
    else:
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
    method = (
        "async_initial_backfill"
        if service == "backfill_history"
        else "async_set_cumulative_reset_at"
    )
    assert getattr(first, method).await_count == (1 if target in (None, "first") else 0)
    assert getattr(second, method).await_count == (1 if target is None else 0)


async def test_reset_action_interprets_naive_timestamp_in_local_timezone(hass):
    coordinator = MagicMock()
    coordinator.async_set_cumulative_reset_at = AsyncMock()
    hass.data[DOMAIN] = {"first": coordinator}
    _async_register_services(hass)
    with patch.object(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo("Europe/Tallinn")):
        await hass.services.async_call(
            DOMAIN,
            "set_cumulative_reset_at",
            {"reset_at": "2026-05-18T12:00:00", "entry_id": "first"},
            blocking=True,
        )
    coordinator.async_set_cumulative_reset_at.assert_awaited_once_with(
        datetime(2026, 5, 18, 9, tzinfo=UTC)
    )


async def test_setup_rejected_credentials_trigger_auth_failure(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_CLIENT_ID: "cid", CONF_CLIENT_SECRET: "secret"}
    )
    with (
        patch(
            "custom_components.estfeed.EstfeedClient.list_metering_points",
            new=AsyncMock(side_effect=EstfeedAuthError("Rejected key")),
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, entry)


@pytest.mark.parametrize(
    "legacy_options",
    [{}, {CONF_VAT_PERCENT: 24.0, CONF_MARGIN_EUR_PER_KWH: 0.0062}],
)
async def test_migration_preserves_legacy_tariff_numbers(hass, legacy_options):
    """Entries from 0.2.x priced both directions as spot * (1 + VAT) + margin
    (VAT default 22%). After migration the new model must give identical
    prices so existing cost statistics stay consistent."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "cid", CONF_CLIENT_SECRET: "secret"},
        options=legacy_options,
        version=1,
        minor_version=1,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 2
    assert entry.options[CONF_VAT_MODE] == VAT_MODE_CUSTOM
    vat = legacy_options.get(CONF_VAT_PERCENT, 22.0)
    margin = legacy_options.get(CONF_MARGIN_EUR_PER_KWH, 0.0)
    config = PricingConfig.from_options(entry.options)
    hour = datetime(2026, 5, 1, tzinfo=UTC)
    for spot in (-0.01, 0.0, 0.0873):
        legacy = spot * (1 + vat / 100) + margin
        assert config.tariff_for(Kind.CONSUMPTION)(hour, spot) == pytest.approx(legacy)
        assert config.tariff_for(Kind.PRODUCTION)(hour, spot) == pytest.approx(legacy)
    assert entry.options[CONF_PRODUCTION_VAT] is True
    assert entry.options[CONF_PRODUCTION_FEE_EUR_PER_KWH] == pytest.approx(
        -entry.options[CONF_MARGIN_EUR_PER_KWH]
    )


async def test_migration_refuses_entries_from_newer_versions(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=2)
    entry.add_to_hass(hass)
    assert not await async_migrate_entry(hass, entry)


async def test_migration_leaves_current_entries_untouched(hass):
    options = {CONF_VAT_MODE: "estonia"}
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert dict(entry.options) == options


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # Adding a winter tariff recalculates from its first day only.
        (
            {CONF_MARGIN_EUR_PER_KWH: 0.01},
            {
                CONF_MARGIN_EUR_PER_KWH: 0.01,
                CONF_SUPPLIER_TARIFFS: "- from: 2026-12-01\n  fixed_price: 0.15\n",
            },
            datetime(2026, 11, 30, 22, tzinfo=UTC),  # 00:00 Tallinn
        ),
        # Changing the base margin recalculates everything (since=None).
        ({CONF_MARGIN_EUR_PER_KWH: 0.01}, {CONF_MARGIN_EUR_PER_KWH: 0.02}, None),
    ],
)
async def test_options_update_rebuilds_from_first_changed_day(hass, old, new, expected):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=new)
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.options = dict(old)
    coordinator.async_rebuild_cost = AsyncMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await _async_options_updated(hass, entry)
    await hass.async_block_till_done()

    coordinator.async_rebuild_cost.assert_awaited_once()
    since = coordinator.async_rebuild_cost.await_args.args[0]
    assert since == expected


async def test_options_update_with_equivalent_periods_does_not_rebuild(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={CONF_SUPPLIER_TARIFFS: "# just a comment\n", CONF_MARGIN_EUR_PER_KWH: 0.01},
    )
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.options = {CONF_MARGIN_EUR_PER_KWH: 0.01}
    coordinator.async_rebuild_cost = AsyncMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await _async_options_updated(hass, entry)
    await hass.async_block_till_done()

    coordinator.async_rebuild_cost.assert_not_called()
