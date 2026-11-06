# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

### Added

- **Tariff periods** option: a dated list of consumption tariffs, so contract changes apply from the right day and earlier history keeps the old price. Each period is either spot + margin or a **fixed price**.
- Optional **day/night network charges** per kWh (day = Monday to Friday 07:00–22:00 Estonian time, except public holidays) and a **monthly fee** spread evenly over the hours of the month.
- Estonian public holiday calendar (including Easter-based holidays), used for the day/night split.

### Changed

- Changing pricing options now recalculates costs only from the first day whose price changed. Changing VAT, production settings or the plain margin still recalculates the whole window.

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
