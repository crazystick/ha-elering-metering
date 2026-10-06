"""Config flow for the Estfeed integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import EstfeedAuthError, EstfeedClient, EstfeedError, EstfeedLoginError
from .const import (
    CONF_BACKFILL_MONTHS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    CONF_MARGIN_EUR_PER_KWH,
    CONF_PRODUCTION_FEE_EUR_PER_KWH,
    CONF_PRODUCTION_VAT,
    CONF_RESOLUTION,
    CONF_VAT_MODE,
    CONF_VAT_PERCENT,
    CUSTOMER_PORTAL_URL,
    DEFAULT_BACKFILL_MONTHS,
    DEFAULT_FRIENDLY_NAME,
    DEFAULT_MARGIN_EUR_PER_KWH,
    DEFAULT_PRODUCTION_FEE_EUR_PER_KWH,
    DEFAULT_PRODUCTION_VAT,
    DEFAULT_VAT_MODE,
    DEFAULT_VAT_PERCENT,
    DOMAIN,
    MAX_BACKFILL_MONTHS,
    MIN_BACKFILL_MONTHS,
    VAT_MODE_CUSTOM,
    VAT_MODE_ESTONIA,
    Resolution,
)
from .utils import slugify

_LOGGER = logging.getLogger(__name__)

_TEXT = TextSelector()
_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _eur_per_kwh(minimum: float, maximum: float) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step="any",
            unit_of_measurement="EUR/kWh",
            mode=NumberSelectorMode.BOX,
        )
    )


def _credentials_schema(defaults: Mapping[str, Any], *, with_name: bool) -> vol.Schema:
    client_id = defaults.get(CONF_CLIENT_ID)
    fields: dict[Any, Any] = {
        (
            vol.Required(CONF_CLIENT_ID, default=client_id)
            if client_id
            else vol.Required(CONF_CLIENT_ID)
        ): _TEXT,
        # Never pre-filled: the secret is not sent back to the browser.
        vol.Required(CONF_CLIENT_SECRET): _PASSWORD,
    }
    if with_name:
        fields[
            vol.Required(
                CONF_FRIENDLY_NAME,
                default=defaults.get(CONF_FRIENDLY_NAME, DEFAULT_FRIENDLY_NAME),
            )
        ] = _TEXT
    return vol.Schema(fields)


def _pricing_schema(current: Mapping[str, Any], *, include_resolution: bool) -> vol.Schema:
    """Pricing and history fields shared by the setup and options flows."""
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_VAT_MODE, default=current.get(CONF_VAT_MODE, DEFAULT_VAT_MODE)
        ): SelectSelector(
            SelectSelectorConfig(
                options=[VAT_MODE_ESTONIA, VAT_MODE_CUSTOM],
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_VAT_MODE,
            )
        ),
        vol.Required(
            CONF_VAT_PERCENT, default=current.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)
        ): NumberSelector(
            NumberSelectorConfig(
                min=0, max=100, step=0.1, unit_of_measurement="%", mode=NumberSelectorMode.BOX
            )
        ),
        vol.Required(
            CONF_MARGIN_EUR_PER_KWH,
            default=current.get(CONF_MARGIN_EUR_PER_KWH, DEFAULT_MARGIN_EUR_PER_KWH),
        ): _eur_per_kwh(-1.0, 1.0),
        vol.Required(
            CONF_PRODUCTION_VAT,
            default=current.get(CONF_PRODUCTION_VAT, DEFAULT_PRODUCTION_VAT),
        ): BooleanSelector(),
        vol.Required(
            CONF_PRODUCTION_FEE_EUR_PER_KWH,
            default=current.get(
                CONF_PRODUCTION_FEE_EUR_PER_KWH, DEFAULT_PRODUCTION_FEE_EUR_PER_KWH
            ),
        ): _eur_per_kwh(-1.0, 1.0),
        vol.Required(
            CONF_BACKFILL_MONTHS,
            default=current.get(CONF_BACKFILL_MONTHS, DEFAULT_BACKFILL_MONTHS),
        ): NumberSelector(
            NumberSelectorConfig(
                min=MIN_BACKFILL_MONTHS,
                max=MAX_BACKFILL_MONTHS,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
    if include_resolution:
        fields[
            vol.Required(
                CONF_RESOLUTION,
                default=current.get(CONF_RESOLUTION, Resolution.HOUR.value),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[Resolution.HOUR.value, Resolution.QUARTER_HOUR.value],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key=CONF_RESOLUTION,
            )
        )
    return vol.Schema(fields)


def _normalize_pricing(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce selector output (floats from number boxes) into stored types."""
    data = dict(user_input)
    data[CONF_VAT_PERCENT] = float(data[CONF_VAT_PERCENT])
    data[CONF_MARGIN_EUR_PER_KWH] = float(data[CONF_MARGIN_EUR_PER_KWH])
    data[CONF_PRODUCTION_FEE_EUR_PER_KWH] = float(data[CONF_PRODUCTION_FEE_EUR_PER_KWH])
    data[CONF_PRODUCTION_VAT] = bool(data[CONF_PRODUCTION_VAT])
    data[CONF_BACKFILL_MONTHS] = int(data[CONF_BACKFILL_MONTHS])
    return data


def _clean_credentials(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Strip whitespace that commonly sneaks in when copy-pasting API keys."""
    data = dict(user_input)
    for key in (CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_FRIENDLY_NAME):
        if isinstance(data.get(key), str):
            data[key] = data[key].strip()
    return data


def _client(hass: HomeAssistant, credentials: Mapping[str, Any]) -> EstfeedClient:
    return EstfeedClient(
        session=async_get_clientsession(hass),
        client_id=credentials[CONF_CLIENT_ID],
        client_secret=credentials[CONF_CLIENT_SECRET],
    )


async def _validate(hass: HomeAssistant, credentials: Mapping[str, Any]) -> None:
    """Prove the credentials work by listing the key's metering points."""
    end = datetime.now(tz=UTC)
    await _client(hass, credentials).list_metering_points(end - timedelta(days=7), end)


class EstfeedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Estfeed."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._credentials: dict[str, Any] = {}

    def _slug_in_use(self, slug: str, *, ignore_entry_id: str | None = None) -> bool:
        """True if another entry's friendly name slugifies to ``slug``.

        Entity unique_ids and statistic_ids are derived from the slug, so a
        collision would silently merge two entries' entities/statistics.
        Config-flow uniqueness is per client_id, so names must be checked
        separately across ALL entries of the domain.
        """
        return any(
            slugify(entry.data.get(CONF_FRIENDLY_NAME, entry.title)) == slug
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id != ignore_entry_id
        )

    def _client_id_in_use(self, client_id: str, *, ignore_entry_id: str) -> bool:
        return any(
            entry.unique_id == client_id
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id != ignore_entry_id
        )

    async def _async_check_credentials(self, credentials: Mapping[str, Any]) -> dict[str, str]:
        try:
            await _validate(self.hass, credentials)
        except EstfeedLoginError as err:
            _LOGGER.warning("Estfeed credential check failed: %s", err)
            if await _client(self.hass, credentials).async_is_datahub_technical_user():
                _LOGGER.warning(
                    "These credentials belong to an Estfeed Datahub technical user, "
                    "which cannot read the customer API. Create an API key in the "
                    "e-Elering customer portal (%s) instead",
                    CUSTOMER_PORTAL_URL,
                )
                return {"base": "datahub_key"}
            return {"base": "invalid_auth"}
        except EstfeedAuthError as err:
            _LOGGER.warning("Estfeed API refused the API key: %s", err)
            return {"base": "no_access"}
        except EstfeedError as err:
            _LOGGER.warning("Could not reach Estfeed during setup: %s", err)
            return {"base": "cannot_connect"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: API credentials and a name for this installation."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _clean_credentials(user_input)
            errors = await self._async_check_credentials(user_input)
            if not errors:
                # unique_id (client_id) abort must run before the slug check
                # so re-adding an existing key aborts instead of tripping
                # over its own friendly name.
                await self.async_set_unique_id(user_input[CONF_CLIENT_ID])
                self._abort_if_unique_id_configured()
                if self._slug_in_use(slugify(user_input[CONF_FRIENDLY_NAME])):
                    errors["base"] = "slug_in_use"
                else:
                    self._credentials = user_input
                    return await self.async_step_pricing()

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(
                {k: v for k, v in (user_input or {}).items() if k != CONF_CLIENT_SECRET},
                with_name=True,
            ),
            errors=errors,
            description_placeholders={"portal_url": CUSTOMER_PORTAL_URL},
        )

    async def async_step_pricing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: tariff settings and how much history to import."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._credentials[CONF_FRIENDLY_NAME],
                data=self._credentials,
                options=_normalize_pricing(user_input),
            )
        return self.async_show_form(
            step_id="pricing",
            data_schema=_pricing_schema({}, include_resolution=False),
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:  # noqa: ARG002
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_credentials_update(
            "reauth_confirm", self._get_reauth_entry(), user_input, "reauth_successful"
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the API key of an existing entry (e.g. after rotating it)."""
        return await self._async_step_credentials_update(
            "reconfigure", self._get_reconfigure_entry(), user_input, "reconfigure_successful"
        )

    async def _async_step_credentials_update(
        self,
        step_id: str,
        entry: ConfigEntry,
        user_input: dict[str, Any] | None,
        reason: str,
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _clean_credentials(user_input)
            full = {**entry.data, **user_input}
            if self._client_id_in_use(full[CONF_CLIENT_ID], ignore_entry_id=entry.entry_id):
                errors["base"] = "already_configured"
            else:
                errors = await self._async_check_credentials(full)
            if not errors:
                if entry.unique_id != full[CONF_CLIENT_ID]:
                    self.hass.config_entries.async_update_entry(
                        entry, unique_id=full[CONF_CLIENT_ID]
                    )
                return self.async_update_reload_and_abort(entry, data=full, reason=reason)

        return self.async_show_form(
            step_id=step_id,
            data_schema=_credentials_schema(entry.data, with_name=False),
            errors=errors,
            description_placeholders={
                "portal_url": CUSTOMER_PORTAL_URL,
                "name": entry.title,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:  # noqa: ARG004
        return EstfeedOptionsFlow()


class EstfeedOptionsFlow(OptionsFlow):
    """Handle Estfeed integration options.

    HA assigns ``self.config_entry`` automatically when instantiating the flow;
    in modern versions it's a read-only property, so don't override __init__.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=_normalize_pricing(user_input))

        return self.async_show_form(
            step_id="init",
            data_schema=_pricing_schema(self.config_entry.options, include_resolution=True),
        )
