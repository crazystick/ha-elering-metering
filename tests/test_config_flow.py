"""Tests for the Estfeed config flow."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.estfeed.api import (
    EstfeedAPIError,
    EstfeedAuthError,
    EstfeedLoginError,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import (
    CONF_BACKFILL_MONTHS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    CONF_MARGIN_EUR_PER_KWH,
    CONF_PRODUCTION_FEE_EUR_PER_KWH,
    CONF_PRODUCTION_VAT,
    CONF_VAT_MODE,
    CONF_VAT_PERCENT,
    DEFAULT_MARGIN_EUR_PER_KWH,
    DEFAULT_VAT_MODE,
    DEFAULT_VAT_PERCENT,
    DOMAIN,
    VAT_MODE_ESTONIA,
    CommodityType,
)


@pytest.fixture(autouse=True)
def _no_entry_setup():
    """Flow tests stop at the config entry; never run the real entry setup,
    and never probe Elering's Datahub login over the network.

    Creating or reloading an entry schedules ``async_setup_entry``. Left
    unpatched it would run after the test's API mocks are gone and race the
    in-memory recorder teardown.
    """
    with (
        patch("custom_components.estfeed.async_setup_entry", return_value=True),
        patch(
            "custom_components.estfeed.config_flow.EstfeedClient.async_is_datahub_technical_user",
            new=AsyncMock(return_value=False),
        ),
    ):
        yield


def _meter() -> MeteringPoint:
    return MeteringPoint(
        eic="38ZEE-00000001-A",
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=UTC), end=None)],
    )


async def _setup_recorder(hass) -> None:
    """Set up the recorder so config-flow init doesn't fail on its dependency."""
    from homeassistant.components import recorder
    from homeassistant.helpers import recorder as recorder_helper

    with patch("homeassistant.components.recorder.ALLOW_IN_MEMORY_DB", True):
        if recorder.DOMAIN not in hass.data:
            recorder_helper.async_initialize_recorder(hass)
        assert await async_setup_component(
            hass,
            recorder.DOMAIN,
            {recorder.DOMAIN: {"db_url": "sqlite://", "commit_interval": 0}},
        )
        await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_user_step_happy_path(hass):
    await _setup_recorder(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_meter()]),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "cid",
                CONF_CLIENT_SECRET: "csec",
                CONF_FRIENDLY_NAME: "Home",
            },
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["step_id"] == "pricing"

    result3 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_VAT_MODE: VAT_MODE_ESTONIA,
            CONF_VAT_PERCENT: 24,
            CONF_MARGIN_EUR_PER_KWH: 0.005,
            CONF_PRODUCTION_VAT: False,
            CONF_PRODUCTION_FEE_EUR_PER_KWH: 0.003,
            CONF_BACKFILL_MONTHS: 6,
        },
    )

    assert result3["type"] == FlowResultType.CREATE_ENTRY
    assert result3["title"] == "Home"
    assert result3["data"] == {
        CONF_CLIENT_ID: "cid",
        CONF_CLIENT_SECRET: "csec",
        CONF_FRIENDLY_NAME: "Home",
    }
    entry = result3["result"]
    assert entry.minor_version == 2
    assert entry.options == {
        CONF_VAT_MODE: VAT_MODE_ESTONIA,
        CONF_VAT_PERCENT: 24.0,
        CONF_MARGIN_EUR_PER_KWH: 0.005,
        CONF_PRODUCTION_VAT: False,
        CONF_PRODUCTION_FEE_EUR_PER_KWH: 0.003,
        CONF_BACKFILL_MONTHS: 6,
    }


@pytest.mark.asyncio
async def test_user_step_strips_pasted_whitespace(hass):
    await _setup_recorder(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_meter()]),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "  cid\n",
                CONF_CLIENT_SECRET: " csec ",
                CONF_FRIENDLY_NAME: " Home ",
            },
        )
    assert result2["step_id"] == "pricing"
    result3 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result3["data"] == {
        CONF_CLIENT_ID: "cid",
        CONF_CLIENT_SECRET: "csec",
        CONF_FRIENDLY_NAME: "Home",
    }
    # Unspecified pricing fields fall back to the documented defaults.
    assert result3["options"][CONF_VAT_MODE] == VAT_MODE_ESTONIA
    assert result3["options"][CONF_PRODUCTION_VAT] is False


@pytest.mark.asyncio
async def test_user_step_form_never_echoes_secret(hass):
    """A failed submission re-renders the form without the secret in it."""
    await _setup_recorder(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(side_effect=EstfeedAuthError("bad")),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "cid",
                CONF_CLIENT_SECRET: "super-secret-value",
                CONF_FRIENDLY_NAME: "Home",
            },
        )
    assert "super-secret-value" not in repr(result2["data_schema"].schema)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (EstfeedLoginError("401: invalid_client"), "invalid_auth"),
        (EstfeedAuthError("403: forbidden"), "no_access"),
        (EstfeedAPIError("503: unavailable"), "cannot_connect"),
    ],
)
async def test_user_step_explains_which_check_failed(hass, caplog, error, expected):
    await _setup_recorder(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with (
        patch(
            "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
            new=AsyncMock(side_effect=error),
        ),
        patch(
            "custom_components.estfeed.config_flow.EstfeedClient.async_is_datahub_technical_user",
            new=AsyncMock(return_value=False),
        ),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "cid",
                CONF_CLIENT_SECRET: "csec",
                CONF_FRIENDLY_NAME: "Home",
            },
        )
    assert result2["errors"] == {"base": expected}
    # Elering's reason is logged so the user can see why, without the secret.
    assert str(error) in caplog.text
    assert "csec" not in caplog.text


@pytest.mark.asyncio
async def test_user_step_bad_credentials_shows_form_error(hass):
    await _setup_recorder(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(side_effect=EstfeedLoginError("401: invalid_client")),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "x",
                CONF_CLIENT_SECRET: "y",
                CONF_FRIENDLY_NAME: "Home",
            },
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_user_step_rejects_colliding_friendly_name(hass):
    """Entity unique_ids and statistic_ids derive from the slugified friendly
    name; a second entry whose name slugifies identically would silently
    collide. The flow must reject it with slug_in_use."""
    await _setup_recorder(hass)
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "one", CONF_CLIENT_SECRET: "s", CONF_FRIENDLY_NAME: "My Home"},
        unique_id="one",
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_meter()]),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            # Different API key, but "my home" slugifies to the same "my_home".
            user_input={
                CONF_CLIENT_ID: "two",
                CONF_CLIENT_SECRET: "s2",
                CONF_FRIENDLY_NAME: "my HOME",
            },
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "slug_in_use"}


@pytest.mark.asyncio
async def test_user_step_accepts_same_entry_updating_name(hass):
    """Re-validating with the same client_id (unique_id match) aborts via
    already_configured before the slug check can false-positive."""
    await _setup_recorder(hass)
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "one", CONF_CLIENT_SECRET: "s", CONF_FRIENDLY_NAME: "Home"},
        unique_id="one",
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_meter()]),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "one",
                CONF_CLIENT_SECRET: "s",
                CONF_FRIENDLY_NAME: "Home",
            },
        )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_reauth_flow_replaces_credentials(hass):
    await _setup_recorder(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "old", CONF_CLIENT_SECRET: "old", CONF_FRIENDLY_NAME: "Home"},
        unique_id="old",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_meter()]),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_CLIENT_ID: "new", CONF_CLIENT_SECRET: "new"},
        )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert entry.data[CONF_CLIENT_ID] == "new"
    assert entry.data[CONF_CLIENT_SECRET] == "new"
    assert entry.unique_id == "new"


@pytest.mark.asyncio
async def test_reconfigure_flow_replaces_credentials(hass):
    await _setup_recorder(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "old", CONF_CLIENT_SECRET: "old", CONF_FRIENDLY_NAME: "Home"},
        unique_id="old",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_meter()]),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_CLIENT_ID: "rotated", CONF_CLIENT_SECRET: "rotated-secret"},
        )
        # The reload is scheduled; let it finish while setup is still patched.
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data[CONF_CLIENT_ID] == "rotated"
    assert entry.data[CONF_CLIENT_SECRET] == "rotated-secret"
    assert entry.data[CONF_FRIENDLY_NAME] == "Home"
    assert entry.unique_id == "rotated"


@pytest.mark.asyncio
async def test_reconfigure_rejects_client_id_of_another_entry(hass):
    await _setup_recorder(hass)
    MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "taken", CONF_CLIENT_SECRET: "s", CONF_FRIENDLY_NAME: "Cabin"},
        unique_id="taken",
    ).add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "mine", CONF_CLIENT_SECRET: "s", CONF_FRIENDLY_NAME: "Home"},
        unique_id="mine",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_CLIENT_ID: "taken", CONF_CLIENT_SECRET: "s"},
    )
    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "already_configured"}
    assert entry.data[CONF_CLIENT_ID] == "mine"


@pytest.mark.asyncio
async def test_options_flow_opens_without_setting_config_entry(hass):
    """Regression: modern HA's OptionsFlow.config_entry is read-only.
    The flow must not try to assign self.config_entry in __init__."""
    await _setup_recorder(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "c", CONF_CLIENT_SECRET: "s", CONF_FRIENDLY_NAME: "Home"},
        options={},
        unique_id="c",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"


@pytest.mark.asyncio
async def test_options_flow_persists_vat_and_margin(hass):
    """Options form accepts VAT% and margin and stores them in entry.options."""
    await _setup_recorder(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "x", CONF_CLIENT_SECRET: "y", CONF_FRIENDLY_NAME: "Home"},
        options={},
        unique_id="x",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    submission = {
        "resolution": "one_hour",
        "backfill_months": 12,
        CONF_VAT_PERCENT: 24.0,
        CONF_MARGIN_EUR_PER_KWH: 0.015,
        CONF_PRODUCTION_VAT: True,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], submission)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_VAT_PERCENT] == 24.0
    assert entry.options[CONF_MARGIN_EUR_PER_KWH] == 0.015
    assert entry.options[CONF_PRODUCTION_VAT] is True


@pytest.mark.asyncio
async def test_options_flow_defaults_to_estonian_vat_and_zero_margin(hass):
    await _setup_recorder(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "x", CONF_CLIENT_SECRET: "y", CONF_FRIENDLY_NAME: "Home"},
        options={},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]
    rendered = {
        k.schema: k.default()
        for k in schema.schema
        if hasattr(k, "default") and hasattr(k, "schema")
    }
    assert rendered[CONF_VAT_MODE] == DEFAULT_VAT_MODE
    assert rendered[CONF_VAT_PERCENT] == DEFAULT_VAT_PERCENT
    assert rendered[CONF_MARGIN_EUR_PER_KWH] == DEFAULT_MARGIN_EUR_PER_KWH


async def test_user_step_recognises_datahub_technical_user(hass, caplog):
    """Datahub technical-user credentials are rejected by the customer login;
    setup must say which kind of key to create instead of a generic error."""
    await _setup_recorder(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with (
        patch(
            "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
            new=AsyncMock(side_effect=EstfeedLoginError("401: invalid_client")),
        ),
        patch(
            "custom_components.estfeed.config_flow.EstfeedClient.async_is_datahub_technical_user",
            new=AsyncMock(return_value=True),
        ),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "cid",
                CONF_CLIENT_SECRET: "csec",
                CONF_FRIENDLY_NAME: "Home",
            },
        )
    assert result2["errors"] == {"base": "datahub_key"}
    assert "kliendiportaal.elering.ee" in caplog.text
    assert "csec" not in caplog.text
