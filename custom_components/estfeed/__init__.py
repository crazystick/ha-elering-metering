"""Estfeed Home Assistant integration."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import voluptuous as vol
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import EstfeedAuthError, EstfeedClient, EstfeedError
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    CONF_MARGIN_EUR_PER_KWH,
    CONF_PRODUCTION_FEE_EUR_PER_KWH,
    CONF_PRODUCTION_VAT,
    CONF_VAT_MODE,
    CONF_VAT_PERCENT,
    DOMAIN,
    MAX_BACKFILL_MONTHS,
    MIN_BACKFILL_MONTHS,
    PRICING_OPTION_KEYS,
    VAT_MODE_CUSTOM,
    CommodityType,
)
from .coordinator import EstfeedCoordinator
from .nps import EleringNpsClient
from .pricing import PricingConfig, local_midnight
from .utils import slugify

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

STORAGE_VERSION = 1

# Before config entry minor version 2 the tariff was ``spot * (1 + VAT) + margin``
# for both consumption and production, with VAT defaulting to 22%.
_LEGACY_DEFAULT_VAT_PERCENT = 22.0

SERVICE_BACKFILL = "backfill_history"
SERVICE_BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Optional("months", default=24): vol.All(
            cv.positive_int, vol.Range(min=MIN_BACKFILL_MONTHS, max=MAX_BACKFILL_MONTHS)
        ),
        vol.Optional("entry_id"): cv.string,
    }
)

SERVICE_SET_CUMULATIVE_RESET_AT = "set_cumulative_reset_at"
SERVICE_SET_CUMULATIVE_RESET_AT_SCHEMA = vol.Schema(
    {
        vol.Required("reset_at"): cv.datetime,
        vol.Optional("entry_id"): cv.string,
    }
)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries created by older versions."""
    if entry.version > 1:
        # Created by a newer version of the integration; refuse to downgrade.
        return False
    if entry.minor_version < 2:
        # Translate the legacy single-tariff options into the new model so
        # that existing cost/compensation statistics keep exactly the same
        # numbers: (spot + m/(1+v)) * (1+v) == spot * (1+v) + m. Users can
        # switch to the Estonian VAT schedule from the options afterwards.
        options = dict(entry.options)
        vat = float(options.get(CONF_VAT_PERCENT, _LEGACY_DEFAULT_VAT_PERCENT))
        net_margin = float(options.get(CONF_MARGIN_EUR_PER_KWH, 0.0)) / (1 + vat / 100)
        options.update(
            {
                CONF_VAT_MODE: VAT_MODE_CUSTOM,
                CONF_VAT_PERCENT: vat,
                CONF_MARGIN_EUR_PER_KWH: net_margin,
                CONF_PRODUCTION_VAT: True,
                CONF_PRODUCTION_FEE_EUR_PER_KWH: -net_margin,
            }
        )
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
        _LOGGER.info("Migrated Estfeed entry %s pricing options to version 1.2", entry.title)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Estfeed from a config entry."""
    session = async_get_clientsession(hass)
    client = EstfeedClient(
        session=session,
        client_id=entry.data[CONF_CLIENT_ID],
        client_secret=entry.data[CONF_CLIENT_SECRET],
    )
    slug = slugify(entry.data.get(CONF_FRIENDLY_NAME, entry.title))

    end = datetime.now(tz=UTC)
    start = end - timedelta(days=7)
    try:
        meters = await client.list_metering_points(start, end)
    except EstfeedAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except EstfeedError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug=slug,
        options={**entry.data, **entry.options},
        config_entry=entry,
    )
    coordinator.meters = meters
    coordinator.attach_nps_client(EleringNpsClient(session))
    coordinator.attach_store(Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.baselines"))
    await coordinator.async_load_baselines()

    needs_backfill = True
    recorder = get_instance(hass)
    for meter in meters:
        for stream in coordinator.streams_for(meter):
            existing = await recorder.async_add_executor_job(
                get_last_statistics, hass, 1, stream.statistic_id, True, {"sum"}
            )
            if existing.get(stream.statistic_id):
                needs_backfill = False
                break
        if not needs_backfill:
            break

    needs_cost_backfill = False
    for meter in meters:
        if meter.commodity_type != CommodityType.ELECTRICITY:
            continue
        for cstream in coordinator.cost_streams_for(meter):
            existing = await recorder.async_add_executor_job(
                get_last_statistics, hass, 1, cstream.statistic_id, True, {"sum"}
            )
            if not existing.get(cstream.statistic_id):
                needs_cost_backfill = True
                break
        if needs_cost_backfill:
            break

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()

    if needs_backfill:
        hass.async_create_background_task(
            coordinator.async_initial_backfill(), name=f"{DOMAIN}_initial_backfill"
        )
    else:
        hass.async_create_background_task(
            coordinator.async_warm_cache(), name=f"{DOMAIN}_warm_cache"
        )
        if needs_cost_backfill:
            # Energy stats already exist; only cost is missing — fill cost
            # without rewriting the (correct) energy series.
            hass.async_create_background_task(
                coordinator.async_rebuild_cost(), name=f"{DOMAIN}_cost_initial_fill"
            )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Estfeed config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    old_options = dict(coordinator.options)
    new_options = {**entry.data, **entry.options}
    coordinator.options = new_options

    if any(old_options.get(key) != new_options.get(key) for key in PRICING_OPTION_KEYS):
        # Only recompute from the first day whose price actually changed, so
        # adding a tariff period never rewrites the history before it.
        changed_from = PricingConfig.from_options(new_options).rebuild_from(
            PricingConfig.from_options(old_options)
        )
        if changed_from is None:
            return
        since = None if changed_from == date.min else local_midnight(changed_from)
        hass.async_create_background_task(
            coordinator.async_rebuild_cost(since), name=f"{DOMAIN}_cost_rebuild"
        )


def _service_targets(hass: HomeAssistant, entry_id: str | None) -> list[EstfeedCoordinator]:
    """Resolve loaded entries without widening an invalid explicit target."""
    entries = hass.data.get(DOMAIN, {})
    if entry_id is None:
        return list(entries.values())
    if entry_id not in entries:
        raise ServiceValidationError("The selected Estfeed entry is not loaded")
    return [entries[entry_id]]


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        return

    async def _handle(call: ServiceCall) -> None:
        months = call.data.get("months", 24)
        entry_id = call.data.get("entry_id")
        targets = _service_targets(hass, entry_id)
        for coord in targets:
            # Pass months explicitly instead of mutating coord.options —
            # an in-memory override would be silently reverted the next
            # time the options flow saves.
            await coord.async_initial_backfill(months=months)

    hass.services.async_register(DOMAIN, SERVICE_BACKFILL, _handle, schema=SERVICE_BACKFILL_SCHEMA)

    async def _set_reset_at(call: ServiceCall) -> None:
        reset_at = call.data["reset_at"]
        reset_at = dt_util.as_utc(reset_at)
        entry_id = call.data.get("entry_id")
        targets = _service_targets(hass, entry_id)
        for coord in targets:
            # Restore both consumption and production baselines so the
            # anchor rewind is symmetric with how baselines are captured.
            await coord.async_set_cumulative_reset_at(reset_at)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CUMULATIVE_RESET_AT,
        _set_reset_at,
        schema=SERVICE_SET_CUMULATIVE_RESET_AT_SCHEMA,
    )
