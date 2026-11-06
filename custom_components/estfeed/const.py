"""Constants for the Estfeed integration."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final

DOMAIN: Final = "estfeed"

CONF_CLIENT_ID: Final = "client_id"
CONF_CLIENT_SECRET: Final = "client_secret"
CONF_FRIENDLY_NAME: Final = "friendly_name"
CONF_RESOLUTION: Final = "resolution"
CONF_BACKFILL_MONTHS: Final = "backfill_months"

DEFAULT_FRIENDLY_NAME: Final = "Home"
DEFAULT_BACKFILL_MONTHS: Final = 12
MAX_BACKFILL_MONTHS: Final = 84
MIN_BACKFILL_MONTHS: Final = 1

# Pricing options. Every per-kWh amount is entered excluding VAT; VAT is then
# applied on top, so a VAT-rate change never requires re-entering the margin.
CONF_VAT_MODE: Final = "vat_mode"
CONF_VAT_PERCENT: Final = "vat_percent"
CONF_MARGIN_EUR_PER_KWH: Final = "margin_eur_per_kwh"
CONF_PRODUCTION_VAT: Final = "production_vat"
CONF_PRODUCTION_FEE_EUR_PER_KWH: Final = "production_fee_eur_per_kwh"
# Optional YAML lists of dated consumption tariffs (see pricing.parse_*_periods).
CONF_SUPPLIER_TARIFFS: Final = "supplier_tariffs"
CONF_NETWORK_TARIFFS: Final = "network_tariffs"

VAT_MODE_ESTONIA: Final = "estonia"
VAT_MODE_CUSTOM: Final = "custom"

DEFAULT_VAT_MODE: Final = VAT_MODE_ESTONIA
DEFAULT_VAT_PERCENT: Final = 24.0
DEFAULT_MARGIN_EUR_PER_KWH: Final = 0.0
DEFAULT_PRODUCTION_VAT: Final = False
DEFAULT_PRODUCTION_FEE_EUR_PER_KWH: Final = 0.0
DEFAULT_TARIFFS: Final = ""

# Changing any of these rebuilds the cost/compensation statistics.
PRICING_OPTION_KEYS: Final = (
    CONF_VAT_MODE,
    CONF_VAT_PERCENT,
    CONF_MARGIN_EUR_PER_KWH,
    CONF_PRODUCTION_VAT,
    CONF_PRODUCTION_FEE_EUR_PER_KWH,
    CONF_SUPPLIER_TARIFFS,
    CONF_NETWORK_TARIFFS,
)

UPDATE_INTERVAL: Final = timedelta(hours=1)
ROLLING_CACHE_DAYS: Final = 62
DATA_FRESH_THRESHOLD: Final = timedelta(hours=30)

API_BASE_URL: Final = "https://estfeed.elering.ee"
KEYCLOAK_TOKEN_URL: Final = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
RATE_LIMIT_SECONDS: Final = 5.0
TOKEN_REFRESH_MARGIN_SECONDS: Final = 30
REQUEST_TIMEOUT_SECONDS: Final = 30
MAX_EICS_PER_REQUEST: Final = 10
MAX_DAYS_PER_REQUEST: Final = 31
RECENT_REQUESTS_BUFFER_SIZE: Final = 5

ATTRIBUTION: Final = "Data provided by Elering Estfeed"


class Resolution(StrEnum):
    """API resolution values."""

    QUARTER_HOUR = "fifteen_min"
    HOUR = "one_hour"
    DAY = "one_day"
    WEEK = "one_week"
    MONTH = "one_month"


class Kind(StrEnum):
    """Metering data kind."""

    CONSUMPTION = "consumption"
    PRODUCTION = "production"


class CommodityType(StrEnum):
    """Estfeed commodity types."""

    ELECTRICITY = "ELECTRICITY"
    NATURAL_GAS = "NATURAL_GAS"
