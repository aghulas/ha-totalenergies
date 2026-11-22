# ha-totalenergies — Home Assistant integration for TotalEnergies business customers (pro.totalenergies.fr)

Guide for Claude Code sessions on this repo. Goal: a **read-only** HA integration that brings the
TotalEnergies **business** customer data (electricity and gas supply) into Home Assistant: invoices,
billed consumption per time class, **costs and effective unit prices**, contracts — so that the Energy
dashboard shows real costs next to the quantities measured by the distributors.

## What we are building

- **HA domain**: `totalenergies_pro`. **Repo**: `aghulas/ha-totalenergies` (private first, HACS later). MIT.
- Read `docs/requirements.md` and `docs/api.md` before writing code.

## Where it fits (do not duplicate the distributors)

Quantities with fine time resolution come from the distributors' integrations (siblings in `~/dev`):
`ha-enedis` (`enedis_entreprise`, 5-min load curve), `ha-grdf` (`grdf_conso`, daily gas), `ha-veolia`.
**None of them carries money.** TotalEnergies only knows billing periods (monthly, per time class), so
this integration's value is: **cost statistics** that the Energy dashboard uses as the cost of the
Enedis / GRDF sources (`stat_cost` — HA refuses an entity or a fixed price on an external statistic,
checked live on 22/11/2026), effective €/kWh price entities (cards, automations, sources that are
entities), invoices and contract facts. Billed kWh are imported too, but they are a billing view, not the measurement.
Reference for this pattern in HA core: the `opower` integration (utility consumption + cost statistics).

## Non-negotiable rules

1. **Never retry a refused password.** `POST /login` answers **HTTP 200 with `[]`** (not 401) when the
   credentials are refused: treat any 200 without `token` as a refusal → stop polling for the entry,
   Repairs issue, reauth flow. Unit-test this first (the MCP connector has the same guard).
2. **DataDome**: a 403 whose headers contain `datadome` = anti-bot block → back off until the next
   day, Repairs issue if it persists; never retry in a loop, never try to bypass it.
3. **Poll little**: invoices are monthly — one fetch per day (with jitter) is plenty; ≥ 1 s between calls;
   12-month windows for the POST searches.
4. **Statistics with real dates, idempotent**: billed kWh and costs are placed on the consumption period
   they belong to (spread per day over the period, documented choice), re-import overwrites.
5. **Read-only**: allow-list of routes (see `docs/api.md`); no payment, request or account route.
6. **Secrets & personal data**: password, tokens (id_token + refresh token), account/contract/PRM/PCE
   numbers, invoice numbers, names, addresses, IBAN — never in logs, diagnostics (redact), fixtures or
   the repo. Fixtures are synthetic. No data specific to the pilot site. `git grep` before pushing.

## Reuse (do not rewrite)

- `~/dev/totalenergies-mcp/totalenergies_mcp/` (private): `client.py` (auth, refresh, allow-list,
  DataDome detection, 12-month windows), `modeles.py` (invoice and consumption-line normalisation),
  `detail.py` (contracts from `/web-methods/sites`; parsing of invoice PDFs → unit prices, subscribed /
  reached power, gas CAR — uses `pdftotext`, to port to a pure-Python PDF library for HA).
- Skills: `~/dev/totalenergies-mcp/skills/totalenergies-factures-contrats/SKILL.md` (billing pitfalls:
  échu / échoir / reprise lines, two invoices in one month, CEE coefficient in labels) and
  `.../analyse-contrats-energie/SKILL.md`.
- `~/dev/ha-grdf` — the most mature sibling: copy its layout (`api/`, `statistics.py`, coordinator with
  self-healing re-import, `brand/`, diagnostics, tests, CI, PR workflow, version in `manifest.json`).

## Suggested sessions

1. `api/` (auth with the 200-`[]` guard, refresh, DataDome, accounts, sites, invoices, consumptions,
   PDF download) + synthetic tests; validate once against the real account **with the user present**.
2. Coordinator, statistics (billed kWh and costs per energy, per point, per time class), price entities,
   invoice and contract sensors, config flow + reauth, diagnostics.
3. Invoice PDF parsing (unit prices, power reached) in pure Python — v0.2 unless trivial.
4. Brand assets, README (en + fr), CI green, release.
