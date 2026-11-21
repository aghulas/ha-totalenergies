# Requirements — ha-totalenergies (`totalenergies_pro`)

## Use cases

- See electricity and gas **costs** in HA's Energy dashboard, consistent with the invoices.
- Have an **effective price per kWh** (per energy, optionally per time class) that the Energy dashboard
  applies to the distributor statistics (`enedis_entreprise`, `grdf_conso`).
- Follow invoices: last invoice per energy (date, period, kWh, HT, TTC, taxes), cancelled invoices,
  monthly totals; download the PDF when needed.
- Contract facts at a glance: contract start, status, tariff option (FTA), subscribed power per time
  class, meter, readings frequency — for each delivery point (PRM electricity, PCE gas).

## Configuration

- Config flow: e-mail + password → login (one attempt) → list the billing accounts (`/web-methods/comptes`)
  and delivery points (`/web-methods/sites`) → select. Options: history depth (default 3 years), fetch
  hour, cost spreading method (per day over the billing period — default; or at period end), PDF folder
  (default `/media/totalenergies`, off by default).
- Reauth flow on refusal / expired refresh token.

## Data and entities

| Data | Source | In HA |
|---|---|---|
| Billed kWh per point, period, time class (HPH/HCH/HPE/HCE, gas base) | `POST /web-methods/consommations` | external statistics `totalenergies_pro:<point>_billed_energy[_<class>]` (sum, kWh) |
| Billed amount HT per line | same (`montantHT`) | external statistics `…_cost` (sum, EUR, HT) and `…_cost_ttc` derived from invoices |
| Invoices (number, date, energy, kWh, HT, taxes, VAT, TTC, issue date, cancellation) | `POST /web-methods/factures?` | sensors "last invoice <energy>" (TTC state, attributes), monthly totals statistic |
| Effective price €/kWh (HT and TTC) per energy | computed: billed amount / billed kWh of the last complete period | sensors `sensor.<…>_price_electricity`, `…_price_gas` (unit EUR/kWh) for the Energy dashboard |
| Contracts per point | `GET /web-methods/sites` | diagnostic sensors / device info (start, status, FTA, subscribed kVA, meter) |
| Invoice PDF | `GET /web-methods/download/facture/<n>` | service `totalenergies_pro.download_invoice` (opt-in folder, never overwrite) |
| Session / last fetch / last error | | diagnostic sensors, binary sensor problem, Repairs |

v0.2 (optional): unit prices and reached power read from the invoice PDF (port of `detail.py`).

## Behaviour

- One fetch per day with jitter, plus a manual refresh button; re-import the last 3 months each time
  (corrections, cancellations); backfill on first setup in 12-month windows.
- Cancelled invoices (`numeroFactureAnnulation`) remove / offset the cancelled amounts — document.
- Abonnement "Échu / Échoir / Reprise" lines: count once (see skill) when building costs.

## Acceptance (v0.1)

1. A test proves a refused password (`200 []`) triggers exactly one login, a Repairs issue and the
   reauth flow, and no other call; a DataDome 403 backs off without retry.
2. Costs and billed kWh for a past year equal the sums of `te_factures` / `te_consommations` (MCP
   connector) for the same period (manual check with the user).
3. The price sensors can be selected in the Energy dashboard for the grid and gas sources.
4. Diagnostics contain no token, number, name or address. CI green (hassfest, HACS, ruff, mypy, pytest).
