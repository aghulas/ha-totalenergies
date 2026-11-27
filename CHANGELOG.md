# Changelog

## 0.2.3 — 2026-11-27

- Prepared for the HACS / Marketplace catalogue: `hacs.json` declares the country (France). No change
  to the integration itself.

## 0.2.2 — 2026-11-22

- Electricity network tariff taken from the invoice PDF when the list of delivery points is withheld:
  the segment (C4…) as the state, the connection (BT > 36 kVA…) as an attribute. Invoices already
  read are read again, a few per fetch.

## 0.2.1 — 2026-11-22

- Invoice PDFs of the layout used since November 2026 read ("Consommation <time class>" lines): the
  price of one more kWh was unknown for them, and the period ran to the end of the subscription
  billed in advance.
- Energy savings certificates priced per kWhc (since April 2026) counted in the price of one more
  kWh; it was too low by their price, about 0.008 €/kWh.
- Invoices read by the previous version are read again, a few per fetch.

## 0.2.0 — 2026-11-22

- **Invoice PDFs read**, in pure Python (pypdf in layout mode; on 26 real invoices the reading equals
  the `pdftotext` one the MCP connector uses). One download per invoice (again only when the reading changes): the
  last 12 months are read a few a day, then each new invoice. Option *Read the invoice PDFs* (on by default).
- New sensors: **price of one more kWh** excl. VAT (supply, capacity, energy savings certificates,
  network variable part, excise; per time class as attributes — an invoice billing two periods with
  two prices is handled), **maximum power reached** over 12 months (electricity: per class, subscribed
  power, overrun hours), gas **annual reference consumption** (network tariff, profile, zone,
  conversion factor, offer).
- When the list of delivery points is withheld, the network tariff, tariff option and subscribed power
  are taken from the last invoice PDF.
- A PDF that cannot be read is tried at the next fetches, then left aside; it never fails the fetch.

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
