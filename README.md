# Elering Estfeed for Home Assistant

[![Validate](https://img.shields.io/github/actions/workflow/status/mikkmihkel/ha-elering-metering/validate.yml?branch=main&label=validate)](https://github.com/mikkmihkel/ha-elering-metering/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-custom-41BDF5)](https://hacs.xyz/docs/faq/custom_repositories/)
[![Release](https://img.shields.io/github/v/release/mikkmihkel/ha-elering-metering)](https://github.com/mikkmihkel/ha-elering-metering/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Eesti keeles:** [juhend allpool](#eesti-keeles) · Elering Estfeedi elektri- ja gaasiarvesti andmed Home Assistanti energiapaneelile.

Imports your **metered electricity and gas consumption** from Elering Estfeed, Estonia's energy data hub, into Home Assistant. Electricity cost and solar compensation are calculated from the Nord Pool spot price with the Estonian VAT rate in force on each day.

- The same hourly (or 15-minute) readings your network operator bills you for, with up to 7 years of history.
- Long-term statistics for the **Energy dashboard**: consumption, return to grid, cost and compensation.
- Optional dated **supplier and network tariffs**: fixed prices, day/night rates, monthly fees.
- Set up in the UI. Talks only to Elering. No extra Python packages, no telemetry.

> Estfeed data arrives about a day late, after the network operator has settled it. Use this for consumption history and costs, not for live power.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Setup](#setup)
- [Energy dashboard](#energy-dashboard)
- [Costs](#costs)
- [Options](#options)
- [Entities](#entities)
- [Actions](#actions)
- [Examples](#examples)
- [Privacy and security](#privacy-and-security)
- [Troubleshooting](#troubleshooting)
- [Eesti keeles](#eesti-keeles)
- [Development](#development)

## Requirements

- Home Assistant 2024.12 or newer, with the recorder enabled (the default).
- An API key (client ID and client secret) from the **[e-Elering customer portal](https://kliendiportaal.elering.ee/)**. Log in, create an API key and copy both values right away.

> Use the **customer portal**. A *technical user* from the Estfeed Datahub portal (`estfeed.elering.ee`) also has a client ID and secret, but it cannot read your meter data. Setup detects this and tells you.

## Installation

### HACS (recommended)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mikkmihkel&repository=ha-elering-metering&category=integration)

Or manually in HACS:

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/mikkmihkel/ha-elering-metering` with type **Integration**.
3. Search for **Elering Estfeed**, select **Download** and restart Home Assistant.

### Manual

Copy `custom_components/estfeed` from the [latest release](https://github.com/mikkmihkel/ha-elering-metering/releases/latest) into `config/custom_components/` and restart Home Assistant.

## Setup

[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=estfeed)

Or open **Settings → Devices & services → Add integration** and search for **Elering Estfeed**.

1. **Connect.** Enter the **client ID**, **client secret** and an **installation name** such as `Home`. The key is checked with Elering before anything is saved. The name becomes part of the statistic IDs, so pick it once.
2. **Electricity price.** Enter your contract terms (all amounts **excluding VAT**, in EUR):

   | Setting | Default | Enter |
   | --- | --- | --- |
   | VAT rate | Estonian (date-aware) | Keep the default. |
   | Seller margin | 0 EUR/kWh | Your seller's margin, e.g. `0.0050` for 0.5 cents/kWh. |
   | Add VAT to production compensation | Off | On only if you are VAT-registered and your buyer pays VAT on sold energy. |
   | Production fee | 0 EUR/kWh | What your buyer deducts per sold kWh. |
   | History to import | 12 months | 1–84 months. |

History is imported in the background. Elering allows one request every 5 seconds, so a year takes about a minute per meter.

Network fees, fixed-price contracts and day/night rates are set afterwards under **Configure**, see [Supplier and network tariffs](#supplier-and-network-tariffs).

## Energy dashboard

Open **Settings → Dashboards → Energy** and add these statistics. `<name>` is the installation name in lowercase, `<id>` the last four characters of the meter's EIC code in lowercase.

| Energy dashboard | Energy statistic | Cost statistic |
| --- | --- | --- |
| Grid consumption | `estfeed:<name>_consumption_<id>` | `estfeed:<name>_cost_<id>` |
| Return to grid | `estfeed:<name>_production_<id>` | `estfeed:<name>_compensation_<id>` |
| Gas consumption | `estfeed:<name>_consumption_<id>` | — |

For the cost, choose **Use an entity tracking the total costs** and pick the cost statistic.

**Example.** Installation `Home`, electricity meter EIC ending in `001A`:

- Grid consumption: `estfeed:home_consumption_001a`, cost `estfeed:home_cost_001a`
- Return to grid: `estfeed:home_production_001a`, compensation `estfeed:home_compensation_001a`

In the statistic picker, type `estfeed` to list them.

## Costs

```text
Cost          = (spot + margin) × (1 + VAT)
Compensation  = (spot − production fee) × (1 + VAT, only if enabled)
```

Spot prices are the Estonian Nord Pool prices published by Elering. With **hourly** data, the hour's quarter-hour prices are averaged. With **15-minute** data, each quarter hour is priced at its own spot price, as Estonian sellers bill since October 2025. This also applies when costs are recalculated after a price change.

With the default VAT setting, each hour uses the rate in force on that day in Estonian time:

| From | VAT |
| --- | --- |
| 1 July 2009 | 20% |
| 1 January 2024 | 22% |
| 1 July 2025 | 24% |

With only a margin, the cost is the **energy** part of your bill. Add tariffs to include network fees, state charges and monthly fees. Gas has no cost statistic.

### Supplier and network tariffs

Under **Settings → Devices & services → Elering Estfeed → Configure** there are two optional text fields, **Supplier tariffs** and **Network tariffs**. Each takes a YAML list of periods. A period applies from its `from` date (Estonian time) until the next period in the same list. All amounts are EUR **excluding VAT**.

**Supplier tariffs** (your electricity seller). Each period has exactly one of `spot_margin`, `fixed_price` or `fixed_day` + `fixed_night`, plus an optional `monthly_fee`. Before the first period, the plain margin applies.

```yaml
- from: 2025-07-01
  spot_margin: 0.0092   # spot contract: spot + margin
- from: 2026-12-01
  fixed_day: 0.1121     # fixed contract with day/night prices
  fixed_night: 0.0960
  monthly_fee: 1.61
- from: 2027-12-01
  fixed_price: 0.1050   # fixed contract, one price
  monthly_fee: 1.63
```

**Network tariffs** (network operator and state charges). Keys: `day`, `night`, `per_kwh` (every hour), `monthly_fee`. All optional. Before the first period, no network charges are added.

```yaml
- from: 2026-06-01
  day: 0.0369           # two-rate network package
  night: 0.0210
  per_kwh: 0.0181       # excise + renewable energy + supply security charges
  monthly_fee: 20.64
```

The values above are examples. Copy yours from your seller's contract and your network operator's price list.

**Day** is Monday to Friday 07:00–22:00 Estonian time, all year, except public holidays. Every other hour is **night**. This matches Elektrilevi's current price list. Monthly fees are spread evenly over the hours of the month.

```text
Cost of an hour = ( kWh × (supplier price + network day/night + per_kwh)
                  + (supplier fee + network fee) ÷ hours in month ) × (1 + VAT)
```

Simplest bill-accurate setup: leave **Supplier tariffs** empty, keep your seller margin in the margin field, and put all network and state per-kWh charges in one network period:

```yaml
- from: 2025-01-01
  per_kwh: 0.0750
  monthly_fee: 5.00
```

Tariffs affect consumption cost only, not production compensation. When you change them, costs are recalculated from the first day whose price changed. Earlier history is kept.

## Options

**Configure** on the integration entry:

| Option | Description |
| --- | --- |
| VAT rate, custom VAT rate | Estonian schedule (default) or a fixed rate. |
| Seller margin | Added to the spot price, excl. VAT. Negative values allowed. |
| Add VAT to production compensation, production fee | Pricing for energy you sell. |
| History to import | Window used when costs are recalculated. |
| Data resolution | **Hourly** (default) or **15 minutes**. 15-minute data makes more API calls and prices each quarter hour at its own spot price. Statistics are always hourly. |
| Supplier tariffs, network tariffs | See [above](#supplier-and-network-tariffs). |

Changing a price setting recalculates the cost statistics in the background. To replace the API key, use **⋮ → Reconfigure**. If Elering rejects the key later, Home Assistant asks you to sign in again.

## Entities

Each metering point becomes a device, for example *Home electricity meter 001A*:

| Entity | Description |
| --- | --- |
| Consumption today / yesterday / month-to-date / previous month | Totals in Home Assistant's time zone. |
| Cumulative consumption | Total since setup or the last reset. Has a **Reset** button. |
| Production sensors and reset button | Same for energy returned to the grid. Disabled by default; enable them if you have solar panels. |
| Latest interval | Time of the newest reading (diagnostic). |
| Data fresh | On while the newest reading is less than 30 hours old (diagnostic). |

The period sensors keep the last 62 days of readings in memory. Cumulative baselines survive restarts.

## Actions

| Action | Fields | Effect |
| --- | --- | --- |
| `estfeed.backfill_history` | `months` (1–84, default 24), `entry_id` (optional) | Re-imports energy and cost statistics for the window. |
| `estfeed.set_cumulative_reset_at` | `reset_at`, `entry_id` (optional) | Sets the starting point of the cumulative sensors. |

Without `entry_id` an action applies to every Estfeed installation. A `reset_at` without a time zone uses Home Assistant's time zone.

```yaml
action: estfeed.backfill_history
data:
  months: 36
```

```yaml
action: estfeed.set_cumulative_reset_at
data:
  reset_at: "2026-01-01T00:00:00"
```

## Examples

Entity IDs below are for installation `Home` and meter `001A`. Check yours under **Settings → Entities**.

**Notify when Elering stops sending data:**

```yaml
automation:
  - alias: Estfeed data is late
    triggers:
      - trigger: state
        entity_id: binary_sensor.home_electricity_meter_001a_data_fresh
        to: "off"
        for: "06:00:00"
    actions:
      - action: notify.notify
        data:
          message: No new meter readings from Elering Estfeed for 36 hours.
```

**Daily cost and consumption for the last 30 days** (dashboard card):

```yaml
type: statistics-graph
title: Electricity per day
chart_type: bar
period: day
days_to_show: 30
stat_types:
  - change
entities:
  - entity: estfeed:home_consumption_001a
    name: Consumption (kWh)
  - entity: estfeed:home_cost_001a
    name: Cost (EUR)
```

**Monthly consumption for the last year:**

```yaml
type: statistics-graph
title: Electricity per month
chart_type: bar
period: month
days_to_show: 365
stat_types:
  - change
entities:
  - estfeed:home_consumption_001a
```

## Privacy and security

- **Network access.** HTTPS only, to `kc.elering.ee` (sign-in), `estfeed.elering.ee` (meter data) and `dashboard.elering.ee` (public spot prices). Nothing else.
- **Credentials.** Stored in Home Assistant's config entry storage and sent only to Elering's sign-in service. If the customer sign-in rejects a key during setup, the same key is tried once against Elering's Datahub sign-in on the same server, only to tell you which kind of key you have; that token is discarded. Protect your backups: they contain the credentials.
- **Diagnostics and logs.** Diagnostics redact the API key and meter EIC codes. Logs show only the last four characters of an EIC. Still review anything you post publicly.
- **Least privilege.** Create a separate API key for Home Assistant and revoke it in the customer portal when you stop using the integration.

Report vulnerabilities privately, see [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| *Elering's login service rejected this client ID and secret* | Create the key in the [e-Elering customer portal](https://kliendiportaal.elering.ee/). Copy both values again and check they are not swapped. The log line `Estfeed credential check failed` shows Elering's reason. |
| *These are Estfeed Datahub technical-user credentials* | Create an API key in the e-Elering customer portal instead. |
| *Elering accepted the API key but refused access* | Check the key is active and linked to your metering points in the customer portal. |
| Sensors are *unavailable* after setup | Wait for the history import. The first readings can take a few minutes. |
| *Data fresh* is off | No new readings for over 30 hours, usually a delay at the network operator. |
| Supplier or network tariffs are not saved | The form shows the error, for example a missing `from` or two price modes in one period. Check the YAML indentation against the examples. |
| Costs look wrong | Amounts must be EUR (not cents) **excluding VAT**. Download diagnostics from the integration page to see the cached spot prices. |

To remove the integration, delete it under **Settings → Devices & services**. Its long-term statistics stay until you delete them in **Developer tools → Statistics**.

## Eesti keeles

Integratsioon toob Elering Estfeedist Home Assistanti sinu **elektri- ja gaasiarvesti näidud**: tunni- või 15-minutilised andmed, kuni 7 aastat ajalugu. Elektri kulu ja päikeseenergia tasu arvutatakse Nord Pooli börsihinnast ja igal päeval kehtinud käibemaksumäärast. Andmed jõuavad Estfeedi umbes päevase viivitusega, seega sobib integratsioon tarbimisajaloo ja kulude jälgimiseks, mitte reaalajas võimsuse mõõtmiseks.

### Vajalik

- Home Assistant 2024.12 või uuem.
- API-võti (client ID ja client secret) **[e-Eleringi kliendiportaalist](https://kliendiportaal.elering.ee/)**. Logi sisse, loo API-võti ja kopeeri mõlemad väärtused kohe.

Estfeedi andmevahetusplatvormi (`estfeed.elering.ee`) *tehniline kasutaja* siin ei tööta. Seadistus annab sellest teada.

### Paigaldamine

1. Ava **HACS → ⋮ → Custom repositories**.
2. Lisa `https://github.com/mikkmihkel/ha-elering-metering`, tüüp **Integration**.
3. Otsi **Elering Estfeed**, vali **Download** ja taaskäivita Home Assistant.

Ilma HACSita: kopeeri kaust `custom_components/estfeed` Home Assistanti kausta `config/custom_components/` ja taaskäivita.

### Seadistamine

Ava **Seaded → Seadmed ja teenused → Lisa sidumine** ja otsi **Elering Estfeed**.

1. Sisesta **client ID**, **client secret** ja paigalduse nimi, näiteks `Kodu`. Nimi läheb statistika ID-sse, seega vali see kohe lõplikuna.
2. Sisesta lepingu hinnad eurodes **ilma käibemaksuta**: müüja marginaal (näiteks `0.0050` = 0,5 senti/kWh), müüdud energia tootmistasu ja imporditava ajaloo pikkus. Käibemaksumäär jäta vaikimisi Eesti graafiku peale.

Ajalugu laaditakse taustal, aasta võtab ühe arvesti kohta umbes minuti.

### Energiapaneel

Ava **Seaded → Armatuurlauad → Energia**. Paigalduse `Kodu` ja arvesti `…001A` puhul:

| Energiapaneelis | Statistika | Kulu |
| --- | --- | --- |
| Võrgust tarbimine | `estfeed:kodu_consumption_001a` | `estfeed:kodu_cost_001a` |
| Võrku tagastamine | `estfeed:kodu_production_001a` | `estfeed:kodu_compensation_001a` |

Kulu jaoks vali **Use an entity tracking the total costs** ja kulustatistika. Statistika valikus kirjuta otsingusse `estfeed`, siis kuvatakse need kõik.

### Võrgutasud, fikseeritud hind, päeva- ja öötariif

Ava integratsiooni juures **Seadista**. Väljadesse **Müüja tariifid** ja **Võrgutariifid** saab panna kuupäevaga perioodid (YAML). Periood kehtib `from` kuupäevast järgmise perioodini. Kõik summad on eurodes **ilma käibemaksuta**.

Müüja: igas perioodis täpselt üks hinnaviis: `spot_margin` (börsihind + marginaal), `fixed_price` (fikseeritud hind) või `fixed_day` + `fixed_night` (päeva- ja öövalem); lisaks soovi korral `monthly_fee` (kuutasu).

```yaml
- from: 2025-07-01
  spot_margin: 0.0092
- from: 2026-12-01
  fixed_day: 0.1121
  fixed_night: 0.0960
  monthly_fee: 1.61
```

Võrk: `day` ja `night` (päeva- ja öötariif), `per_kwh` (iga tund, nt aktsiis, taastuvenergia- ja varustuskindluse tasu) ja `monthly_fee`.

```yaml
- from: 2026-06-01
  day: 0.0369
  night: 0.0210
  per_kwh: 0.0181
  monthly_fee: 20.64
```

Need on näidisväärtused. Võta oma hinnad müüja lepingust ja võrguettevõtja hinnakirjast. **Päev** on E–R kell 7–22 Eesti aja järgi aasta ringi, v.a riigipühad, ülejäänud aeg on **öö**. Nii on see Elektrilevi kehtivas hinnakirjas. Kuutasu jagatakse kuu tundide vahel ühtlaselt.

Tariifi muutmisel arvutatakse kulu ümber alates esimesest päevast, mille hind muutus.

### Probleemid

| Teade | Mida teha |
| --- | --- |
| Eleringi sisselogimine lükkas võtme tagasi | Loo võti e-Eleringi kliendiportaalis. Kopeeri ID ja secret uuesti ning kontrolli, et need poleks vahetuses. |
| Tegemist on Estfeedi tehnilise kasutajaga | Loo API-võti hoopis e-Eleringi kliendiportaalis. |
| Võti töötab, aga ligipääs puudub | Kontrolli kliendiportaalis, et võti on aktiivne ja seotud sinu mõõtepunktidega. |
| *Data fresh* on väljas | Uusi näite pole üle 30 tunni. Tavaliselt on viivitus võrguettevõtja poolel. |
| Kulu tundub vale | Hinnad peavad olema eurodes (mitte sentides) ja ilma käibemaksuta. |

Turvaprobleemidest teata privaatselt, vt [SECURITY.md](SECURITY.md).

## Development

Python 3.13.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]' --config-settings editable_mode=compat
ruff check custom_components tests scripts
ruff format --check custom_components tests scripts
mypy
pytest tests --cov=custom_components/estfeed --cov-fail-under=85
```

Test against the live API with your own key (meter IDs are masked in the output):

```bash
ESTFEED_CLIENT_ID=... ESTFEED_CLIENT_SECRET=... python -m scripts.smoke
```

Pull requests are welcome. Keep changes focused, add tests, and run the checks above; CI runs them on every pull request. Do not add third-party Python requirements or new network endpoints without discussing it in an issue first.

## Credits and disclaimer

A fork of [tehisain/ha-estfeed](https://github.com/tehisain/ha-estfeed) by Ain Tehis, under the [MIT License](LICENSE). Thanks to everyone who contributed fixes and features, see the [changelog](CHANGELOG.md).

An independent community project, not affiliated with or endorsed by Elering AS. "Elering" and "Estfeed" are names of Elering AS, used only to describe the data source. Cost figures are estimates, not a substitute for your invoice.
