# Elering Estfeed for Home Assistant

[![Validate](https://img.shields.io/github/actions/workflow/status/mikkmihkel/ha-elering-metering/validate.yml?branch=main&label=validate)](https://github.com/mikkmihkel/ha-elering-metering/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-custom-41BDF5)](https://hacs.xyz/docs/faq/custom_repositories/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Bring your **actual metered electricity and gas consumption** from [Elering Estfeed](https://estfeed.elering.ee/), Estonia's energy data hub, into Home Assistant: the Energy dashboard, history and automations. Electricity costs and solar compensation are calculated from the hourly Nord Pool spot price, with the correct Estonian VAT for every day of your history.

- **Real meter data.** The same hourly readings your network operator bills you for. Up to 7 years of history is imported.
- **Energy dashboard ready.** Long-term statistics for consumption, production (return to grid), cost and compensation.
- **Correct VAT history.** 20% until 2023, 22% from January 2024, 24% from July 2025. Applied per day, in Estonian time.
- **Set up in the UI.** No YAML, no code. Enter your API key and contract prices while adding the integration.
- **Private by design.** Talks only to Elering. No third-party services, no telemetry, no extra Python packages.

> Estfeed data is settled by the network operator and normally arrives about a day late. This is a consumption history and billing integration, not a real-time power monitor.

## Requirements

- Home Assistant 2024.12 or newer, with the recorder enabled (it is by default).
- An Estfeed API key (a client ID and client secret). Log in to the [Estfeed portal](https://estfeed.elering.ee/) and create one. The key can read the metering points your account has access to.

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

Without the tariff lists below, the cost statistics are a close estimate of the **energy** part of your bill. Network fees, the renewable energy charge, excise duty and monthly fees are only included if you add them as a flat amount to the margin. Gas has no cost statistic.

### Supplier and network tariffs (fixed prices, day/night rates, monthly fees)

For a bill-accurate cost, or when you switch between spot and fixed-price contracts, set **Supplier tariffs** and **Network tariffs** in the integration's options. Each is a YAML list of dated periods. A period applies from its `from` date (Estonian time) until the next period in the same list starts, so seller and network prices can change independently. Both lists are optional.

**Supplier tariffs** — your electricity seller. Before the first period, the plain margin applies.

```yaml
- from: 2025-07-01
  spot_margin: 0.0092      # spot price + seller margin and balancing
- from: 2026-12-01         # fixed-price contract with day/night energy prices
  fixed_day: 0.1121
  fixed_night: 0.0960
  monthly_fee: 1.61
- from: 2027-12-01         # single-rate fixed price
  fixed_price: 0.1050
  monthly_fee: 1.63
```

| Key | Meaning |
| --- | --- |
| `from` | First day of the period, `YYYY-MM-DD`. |
| `spot_margin`, `fixed_price`, or `fixed_day` + `fixed_night` | Exactly one. `spot_margin` is added to the hourly spot price; `fixed_price` replaces it in every hour; `fixed_day`/`fixed_night` replace it in day and night hours. |
| `monthly_fee` | Optional EUR per month. |

**Network tariffs** — network operator and state charges. Before the first period, none are added.

```yaml
- from: 2026-06-01         # Elektrilevi Võrk 4
  day: 0.0369
  night: 0.0210
  per_kwh: 0.0181          # excise + renewable energy + supply security charges
  monthly_fee: 20.64
```

| Key | Meaning |
| --- | --- |
| `from` | First day of the period, `YYYY-MM-DD`. |
| `day`, `night` | Per-kWh charges in day and night hours. |
| `per_kwh` | Per-kWh charge in every hour (single-rate packages, state charges). |
| `monthly_fee` | EUR per month. |

All amounts are in EUR **excluding VAT**. *Day* is Monday to Friday 07:00–22:00 Estonian time, except public holidays; *night* is every other hour. This is how Elektrilevi meters day and night, so sellers' day/night prices follow the same hours. Monthly fees are spread evenly over the hours of the month, so daily and weekly costs include their share.

```text
Cost of an hour = (kWh × (supplier price + network day/night + per_kwh)
                   + (supplier fee + network fee) ÷ hours in month) × (1 + VAT)
```

If you don't need the split, leave the supplier list empty and put your whole per-kWh add-on (seller margin plus network and state charges) in the network `per_kwh`.

The tariffs only affect consumption cost, not production compensation. When you change them, costs are recalculated only from the first day whose price changed, so adding a new contract leaves earlier history as it was.

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
- **Least privilege.** Create a dedicated API key for Home Assistant and revoke it in the Estfeed portal if you stop using the integration.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| *Elering rejected the client ID or client secret* | Copy both values again from the Estfeed portal. Make sure the key is still active. |
| Sensors show *unavailable* right after setup | Wait for the background import to finish. The first readings can take a few minutes to appear. |
| *Data fresh* is off | Estfeed has not published new readings for over 30 hours. This is usually a delay at the network operator. |
| Costs look wrong | Check the margin and tariffs are entered **excluding VAT** and in EUR (not cents). Then download diagnostics from the integration page to compare cached prices. |

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
