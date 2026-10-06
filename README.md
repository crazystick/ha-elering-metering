# Elering Estfeed for Home Assistant

[![Validate](https://img.shields.io/github/actions/workflow/status/mikkmihkel/ha-elering-metering/validate.yml?branch=main&label=validate)](https://github.com/mikkmihkel/ha-elering-metering/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-custom-41BDF5)](https://hacs.xyz/docs/faq/custom_repositories/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Bring your **actual metered electricity and gas consumption** from Elering Estfeed, Estonia's energy data hub, into Home Assistant: the Energy dashboard, history and automations. Electricity costs and solar compensation are calculated from the hourly Nord Pool spot price, with the correct Estonian VAT for every day of your history.

- **Real meter data.** The same hourly readings your network operator bills you for. Up to 7 years of history is imported.
- **Energy dashboard ready.** Long-term statistics for consumption, production (return to grid), cost and compensation.
- **Correct VAT history.** 20% until 2023, 22% from January 2024, 24% from July 2025. Applied per day, in Estonian time.
- **Set up in the UI.** No YAML, no code. Enter your API key and contract prices while adding the integration.
- **Private by design.** Talks only to Elering. No third-party services, no telemetry, no extra Python packages.

> Estfeed data is settled by the network operator and normally arrives about a day late. This is a consumption history and billing integration, not a real-time power monitor.

## Requirements

- Home Assistant 2024.12 or newer, with the recorder enabled (it is by default).
- An API key (a client ID and client secret) from the **[e-Elering customer portal](https://kliendiportaal.elering.ee/)**. Log in, create an API key, and copy both values right away. The key can read the metering points your account has access to.

> **Use the customer portal, not the Estfeed Datahub portal.** A *technical user* created at `estfeed.elering.ee` also has a client ID and secret, but those are for market participants and are rejected here. Setup detects this and tells you.

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/mikkmihkel/ha-elering-metering` with type **Integration**.
3. Search for **Elering Estfeed**, select **Download**, then restart Home Assistant.

### Manual

Copy `custom_components/estfeed` from this repository into your Home Assistant `config/custom_components/` folder and restart Home Assistant.

## Setup

Open **Settings → Devices & services → Add integration** and search for **Elering Estfeed**. Setup has two steps.

**1. Connect.** Enter the **client ID** and **client secret** of your Estfeed API key, and a short **installation name** such as `Home`. The key is checked against Elering before anything is saved. The name becomes part of the statistic IDs and cannot be changed later without losing history.

**2. Electricity price.** Enter your contract terms so that costs match your bill:

| Setting | Default | What to enter |
| --- | --- | --- |
| VAT rate | Estonian standard rate (date-aware) | Keep the default unless you have a reason to use a fixed rate. |
| Custom VAT rate | 24% | Used only when *VAT rate* is set to a custom value. |
| Seller margin and other per-kWh charges | 0 EUR/kWh | Your seller's margin **excluding VAT**, e.g. `0.0050` for 0.5 cents/kWh. A flat per-kWh network fee can be added here too. |
| Add VAT to production compensation | Off | Turn on only if you are VAT-registered and your buyer pays VAT on sold energy. |
| Production fee | 0 EUR/kWh | The amount per kWh your buyer deducts from the spot price for energy you sell, excluding VAT. |
| History to import | 12 months | 1–84 months, imported in the background after setup. |

That's it. History import starts right away in the background. Elering allows one API request every 5 seconds and each request covers up to 31 days, so a year of history takes about a minute per meter.

All price settings can be changed later under **Configure**. Changing them recalculates the cost statistics for the history window in the background. To replace the API key (for example after rotating it), use **⋮ → Reconfigure** on the integration entry. If Elering rejects the key, Home Assistant asks you to sign in again.

## Energy dashboard

Open **Settings → Dashboards → Energy** and add your grid connection using these statistics. `<name>` is the installation name in lowercase (for example `home`). `<id>` is the last four characters of the meter's EIC code, in lowercase.

| Energy dashboard setting | Energy statistic | Cost statistic (optional) |
| --- | --- | --- |
| Grid consumption | `estfeed:<name>_consumption_<id>` | `estfeed:<name>_cost_<id>` |
| Return to grid | `estfeed:<name>_production_<id>` | `estfeed:<name>_compensation_<id>` |
| Gas consumption | `estfeed:<name>_consumption_<id>` | — |

For costs, choose **Use an entity tracking the total costs** and pick the matching cost statistic.

### How costs are calculated

Electricity prices are the Estonian Nord Pool spot prices published by Elering. Quarter-hour prices are averaged per hour and multiplied by that hour's metered energy:

```text
Cost          = (spot + margin) × (1 + VAT)
Compensation  = (spot − production fee) × (1 + VAT, only if enabled)
```

With the default Estonian VAT setting, each hour uses the rate in force on that day in Estonian time:

| From | VAT |
| --- | --- |
| 1 July 2009 | 20% |
| 1 January 2024 | 22% |
| 1 July 2025 | 24% |

The cost statistics are a close estimate of the **energy** part of your bill. They do not include network fees with day/night tariffs, the renewable energy charge, excise duty or monthly fixed fees, unless you add them as a flat amount to the margin. Gas has no cost statistic.

## Entities

Each metering point becomes a device, for example *Home electricity meter 001A*, with these entities:

| Entity | Description |
| --- | --- |
| Consumption today / yesterday / month to date / previous month | Totals for each period in Home Assistant's time zone. |
| Cumulative consumption | Total since setup or since the last reset, with a **Reset** button. |
| Production sensors and reset button | The same set for energy returned to the grid. Disabled by default; enable them if you have solar panels. |
| Latest interval | Timestamp of the newest reading received (diagnostic). |
| Data fresh | On while the newest reading is less than 30 hours old (diagnostic). Useful for alerts. |

The period sensors use the last 62 days of readings, kept in memory. Cumulative baselines survive restarts.

## Actions

Run from **Developer tools → Actions** or from automations:

| Action | Fields | Effect |
| --- | --- | --- |
| `estfeed.backfill_history` | `months` (1–84, default 24), optional `entry_id` | Re-import energy and cost statistics for the given window. |
| `estfeed.set_cumulative_reset_at` | `reset_at`, optional `entry_id` | Move the cumulative sensors' starting point to a specific time. |

Without `entry_id`, an action applies to every Estfeed installation. A `reset_at` without a time zone uses Home Assistant's time zone.

## Privacy and security

- **Network access.** The integration only connects, over HTTPS, to `kc.elering.ee` (sign-in), `estfeed.elering.ee` (meter data) and `dashboard.elering.ee` (public spot prices). Nothing is sent anywhere else.
- **Credentials.** The client ID and secret are stored in Home Assistant's config entry storage, like every other integration's credentials, and are sent only to Elering's sign-in service. The secret field is masked in the UI and never shown again. Protect your Home Assistant backups, because they contain these credentials.
- **Diagnostics and logs.** Downloaded diagnostics redact the API key and meter EIC codes. Log messages and error texts show only the last four characters of an EIC. Please still review anything you paste into a public issue.
- **Least privilege.** Create a dedicated API key for Home Assistant and revoke it in the e-Elering customer portal if you stop using the integration.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| *Elering's login service rejected this client ID and secret* | Make sure the key was created in the [e-Elering customer portal](https://kliendiportaal.elering.ee/), not as a technical user at `estfeed.elering.ee`. Copy both values again and check they are not swapped. The log line starting with `Estfeed credential check failed` shows Elering's reason. |
| *These are Estfeed Datahub technical-user credentials* | You created a Datahub technical user. Create an API key in the e-Elering customer portal instead. |
| *Elering accepted the API key but refused access* | The key logs in but cannot read meter data. Check the key is active and linked to your metering points in the customer portal. |
| Sensors show *unavailable* right after setup | Wait for the background import to finish. The first readings can take a few minutes to appear. |
| *Data fresh* is off | Estfeed has not published new readings for over 30 hours. This is usually a delay at the network operator. |
| Costs look wrong | Check the margin is entered **excluding VAT** and in EUR (not cents). Then download diagnostics from the integration page to compare cached prices. |

## Development

Requires Python 3.13.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]' --config-settings editable_mode=compat
ruff check custom_components tests scripts
ruff format --check custom_components tests scripts
mypy
pytest tests --cov=custom_components/estfeed --cov-fail-under=85
```

To test against the live API with your own key (meter IDs are masked in the output):

```bash
ESTFEED_CLIENT_ID=... ESTFEED_CLIENT_SECRET=... python -m scripts.smoke
```

## Credits and disclaimer

This project is a fork of [tehisain/ha-estfeed](https://github.com/tehisain/ha-estfeed) by Ain Tehis, released under the [MIT License](LICENSE).

It is an independent community project. It is not affiliated with, endorsed by or supported by Elering AS. "Elering" and "Estfeed" are names of Elering AS and are used here only to describe the data source. Cost figures are estimates and are not a substitute for your invoice.
