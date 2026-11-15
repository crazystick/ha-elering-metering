# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [semantic versioning](https://semver.org/).

## [0.4.0] - 2026-11-15

### Added

- **Supplier tariffs** and **Network tariffs** options: dated lists of consumption prices, so contract and network price changes apply from the right day and earlier history keeps the old price. Supplier periods can be spot + margin, a fixed price or fixed day/night prices, with an optional monthly fee. Network periods can have day/night and flat per-kWh charges and a monthly fee. Monthly fees are spread evenly over the hours of the month. ([#2](https://github.com/mikkmihkel/ha-elering-metering/pull/2), @crazystick)
- Estonian public holiday calendar, used for the network day/night split (day = Monday to Friday 07:00–22:00 Estonian time, except public holidays).
- Setup recognises Estfeed Datahub technical-user credentials and says which key to create instead.
- README: Estonian instructions, dashboard and automation examples, action examples, an options reference.

### Changed

- With 15-minute data resolution, each quarter hour is priced at its own spot price instead of the hourly average, as Estonian sellers bill since October 2025. Hourly resolution is unchanged. Run `estfeed.backfill_history` to recalculate past costs. ([#4](https://github.com/mikkmihkel/ha-elering-metering/pull/4), @scorphious)
- Changing pricing options recalculates costs only from the first day whose price changed. Changing VAT or production settings still recalculates the whole window. ([#2](https://github.com/mikkmihkel/ha-elering-metering/pull/2))
- Setup links to the [e-Elering customer portal](https://kliendiportaal.elering.ee/), where customer API keys are created, instead of the Estfeed Datahub portal.
- A wrong client ID or secret and a key without access to metering data now get separate setup errors. The log records Elering's reason (for example `invalid_client`), never the secret.

### Fixed

- **Statistics were rewritten every hour.** The last stored statistic time was read as milliseconds although the recorder returns seconds, so every update re-imported the whole 30-day window. ([#3](https://github.com/mikkmihkel/ha-elering-metering/pull/3), @crazystick; also reported in [#5](https://github.com/mikkmihkel/ha-elering-metering/pull/5) by @mnsrt)

### Security

- All contributed changes were reviewed before merging. No new hosts or Python requirements. The only new endpoint is the Datahub sign-in on the existing `kc.elering.ee`, used once when setup rejects a key. Tariff text is parsed with YAML `safe_load` and validated before it is saved.
- Releases are published by a workflow when a version tag is pushed. Only that job can write, only to repository contents, and it uses no third-party actions besides the pinned checkout.

## [0.3.0] - 2026-10-05

First release of this fork of [tehisain/ha-estfeed](https://github.com/tehisain/ha-estfeed) (0.2.5).

### Added

- Date-aware Estonian VAT. Each hour is priced with the VAT rate in force on that day in Estonian time: 20% until 2023, 22% from 1 January 2024 and 24% from 1 July 2025. A custom fixed rate is still available.
- Separate production (return to grid) pricing. Compensation is the spot price minus an optional per-kWh fee, without VAT unless you enable it.
- Pricing and history settings during setup, so the first import already uses your contract terms.
- Reconfigure flow to replace the API key without losing history.
- Help text for every setup and options field, in English and Estonian.

### Changed

- **Margin is now entered excluding VAT.** Cost is `(spot + margin) × (1 + VAT)`, so VAT also applies to the margin, matching Estonian invoices.
- Default VAT changed from a fixed 22% to the Estonian schedule.
- Devices are named after the installation, for example *Home electricity meter 001A*, instead of showing the full EIC. The full EIC is kept as the device serial number.
- The client secret field is masked in the UI.
- Integration display name is now *Elering Estfeed*.

### Security

- Log messages, error texts and diagnostics no longer contain full meter EIC codes.
- API error bodies are shortened before they reach logs or diagnostics.
- CI uses a read-only token, pinned action SHAs and Dependabot updates.
- Removed internal planning documents and a real meter ID from the test suite.

### Migration

Existing entries from 0.2.x are migrated automatically and keep their exact prices: the old VAT becomes a custom fixed rate, the old margin is converted to its VAT-exclusive equivalent, and production keeps the old formula. To switch to the Estonian VAT schedule, open **Configure** and save the new settings. Cost statistics are then recalculated.
