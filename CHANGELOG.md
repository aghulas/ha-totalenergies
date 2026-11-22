# Changelog

## 0.1.0 — 2026-11-22

First release.

- Config flow with delivery point selection, re-authentication and options (English and French).
- Lockout-safe sign-in on pro.totalenergies.fr: a refused password is never sent again — every request
  stops, a repair issue is raised and re-authentication is asked for, across restarts. Between fetches
  the session is renewed with its refresh token, not the password.
- Anti-bot block (DataDome) detected: the integration waits a day and raises a repair issue if it
  persists; never a retry loop, never a workaround.
- Read-only by construction: an allow-list of reading routes.
- Billed energy (total and per time class), energy cost and invoice totals excl. / incl. VAT imported as
  long-term statistics, placed on the days of the billed period, idempotently, with backfill of the
  configured history (three years by default) and re-import when the statistics are lost.
- Cancelled invoices excluded; credit notes offset.
- Sensors: last invoice, average price per kWh over 12 months, energy price of the last invoice,
  invoiced over 12 months, contract status, start, network tariff and subscribed power; connection
  problem sensor, rate-limited refresh button.
- Action `download_invoice` to save invoice PDFs to an opt-in folder, never overwriting.
- When TotalEnergies withholds the list of delivery points, the setup offers the points billed in the
  last 12 months and the daily fetch keeps the last known contracts (seen on 22/11/2026).
- Icon and logo in the integration's `brand/` folder: original artwork, not TotalEnergies' logo.
- Checked on a real installation: three years of invoices, totals equal to TotalEnergies' to the cent;
  costs used as the cost of the Enedis and GRDF sources in the Energy dashboard.
