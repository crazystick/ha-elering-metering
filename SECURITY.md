# Security policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report them privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability** ([direct link](https://github.com/mikkmihkel/ha-elering-metering/security/advisories/new)). Include the version, what an attacker could do, and steps to reproduce.

You can expect an acknowledgement within a week. Once a fix is released, the advisory is published with credit to you unless you prefer otherwise.

## Handling your data in reports

Never post your Estfeed **client secret** anywhere. If you share diagnostics or logs, download diagnostics from the integration page (secrets and meter codes are redacted automatically) and check the content before attaching it. If a secret was exposed, revoke the API key in the [e-Elering customer portal](https://kliendiportaal.elering.ee/) and create a new one.
