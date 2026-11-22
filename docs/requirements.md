# Requirements — ha-totalenergies (`totalenergies_pro`)

## Use cases

- See electricity and gas **costs** in HA's Energy dashboard, consistent with the invoices.
- Use the billed **cost statistics** as the cost of the distributor sources (`enedis_entreprise`,
  `grdf_conso`) in the Energy dashboard: their energy statistics are external, and Home Assistant only
  accepts a cost statistic (`stat_cost`) on an external statistic — not an entity with the current price
  nor a fixed price ("Entity or number price is not supported for external statistics", 22/11/2026).
- Have an **effective price per kWh** (per energy, optionally per time class) for cards, automations and
  Energy sources that are entities.
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
| Billed kWh per point, period, time class (HPH/HCH/HPE/HCE, gas base) | `POST /web-methods/consommations` | external statistics `totalenergies_pro:<point>_billed_energy` and `…_billed_energy_<class>` (sum, kWh) |
| Billed amount HT per line (energy part) | same (`montantHT`) | external statistic `…_energy_cost` (sum, EUR excl. tax) |
| Invoice totals (all components) | `POST /web-methods/factures?` | external statistics `…_cost_excl_vat` and `…_cost_incl_vat` (sum, EUR), spread over the periods of the invoice's lines |
| Invoices (number, date, energy, kWh, HT, taxes, VAT, TTC) | same | sensors "last invoice" (TTC state, attributes), "last invoice date", "last invoice energy", "invoiced over 12 months" |
| Price €/kWh per point | computed (`billing.py`) | sensors "average price excl./incl. VAT" (all-in, **last 12 months** — a single summer gas invoice is mostly fixed charges) for cards, automations and Energy sources that are entities; "energy price" (energy part of the last invoice, per class in attributes) |
| Contracts per point | `GET /web-methods/sites` | diagnostic sensors: status, start, network tariff (FTA), subscribed power (kVA, per class in attributes) |
| Invoice PDF | `GET /web-methods/download/facture/<n>` | service `totalenergies_pro.download_invoice` (opt-in folder, never overwrite, default: last invoice of each point) |
| Session / last fetch / last error | | diagnostic sensors, binary sensor problem, Repairs |

v0.2 (optional): unit prices and reached power read from the invoice PDF (port of `detail.py`).

## Behaviour

- One fetch per day with jitter, plus a manual refresh button; each fetch searches the last 12 months
  (one call per search) and re-imports them except their oldest 92 days; backfill on first setup, when
  the history depth or the spreading method changes, or when the statistics were lost.
- Cancelled invoices (`numeroFactureAnnulation` or `dateImpressionAnnulation` set) and their lines count
  for nothing; credit notes with negative amounts offset naturally. To check on the first real one.
- Abonnement "Échu / Échoir / Reprise" lines only exist in the PDFs. The invoice totals used for the
  costs include them as billed: an invoice can carry the next period's fixed part and take back the
  previous one, so a single month may be off by that difference; yearly totals are exact. An invoice
  without lines (regularisation) is put on its date.

## Acceptance (v0.1)

1. A test proves a refused password (`200 []`) triggers exactly one login, a Repairs issue and the
   reauth flow, and no other call; a DataDome 403 backs off without retry.
2. Costs and billed kWh for a past year equal the sums of `te_factures` / `te_consommations` (MCP
   connector) for the same period (manual check with the user).
3. The cost statistics are set as the cost of the Enedis and GRDF sources in the Energy dashboard, with
   no validation issue (done 22/11/2026; the price sensors were refused there, see Use cases).
4. Diagnostics contain no token, number, name or address. CI green (hassfest, HACS, ruff, mypy, pytest).
