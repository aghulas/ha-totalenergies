# TotalEnergies business customer API (pro.totalenergies.fr) — as used by the MCP connector

Unofficial, undocumented, may change. Reverse-engineered on 27–28/10/2026 (web front v26.10.3–v26.10.5).
Source of truth: `~/dev/totalenergies-mcp/totalenergies_mcp/client.py`, `modeles.py`, `detail.py`.

## Base and authentication

- Base: `https://api-b2b.totalenergies.fr`. Headers: `Accept: application/json, text/plain, */*`.
- `POST /login` body `{"username": <e-mail>, "password": <password>}` → `{token, oid, refreshToken,
  expiresAt}`. `token` is an Azure AD B2C id_token (~1 h), sent as `Authorization: Bearer <token>`.
  **Refused credentials → HTTP 200 with `[]`** (no 401): any 200 without `token` = refused.
- `POST /refresh-token` body `{"refreshToken": …}` → same shape. Try it before any password login.
- `expiresAt` may be missing: fall back to the JWT `exp`, else now + 50 min.
- Anti-bot: **DataDome** in front of the API; a 403 with a `datadome` header = blocked.
- On 401 during a call: force a refresh / login once, retry once.

## Allow-list (the only calls)

| Method | Path | Body | Use |
|---|---|---|---|
| POST | `/login` | username, password | auth |
| POST | `/refresh-token` | refreshToken | auth |
| GET | `/web-methods/comptes` | — | billing accounts: `comptes[].compte.{numero, nom, segment, modeEnvoiFacture}` |
| GET | `/web-methods/sites` | — | delivery points and contracts (see below) |
| POST | `/web-methods/factures?` (trailing `?` sent by the front and the MCP connector; aiohttp cannot send an empty query, the integration calls `/web-methods/factures` — to confirm live, `scripts/live_check.py`) | `{dateDebutPeriode, dateFinPeriode}` (YYYY-MM-DD, ≤ 12 months) | invoices |
| POST | `/web-methods/consommations` | `{dateDebutRecherche, dateFinRecherche}` (≤ 12 months) | billed consumption lines |
| GET | `/web-methods/download/facture/<numero>` | — | invoice PDF (`%PDF` bytes) |

## Invoices — `factures[].facture` (or flat)

`numero`, `date`, `secteurActivite` (`01` electricity, `02` gas), `type`, `descriptifCompteContrat`,
`descriptifContrat`, `idEnseigne`, `nbPOD`, `consoTotalKWH`, `montantSousTotalHT`, `sommeTaxes`,
`montantTotalHorsTVA`, `montantTVA`, `montantTTC`, `dateImpression`, `numeroFactureAnnulation`,
`dateImpressionAnnulation`, `modeEnvoi`, `statutChorus`. Numbers may be strings with a comma decimal.
Dates `0000-…` mean empty. Response flag `isHeavyLoad`. De-duplicate by `numero` across windows.

## Consumption lines — `consumptions[]`

`pod` (PRM / PCE), `energie` (`01` / `02`), `dateDebutPeriode`, `dateFinPeriode`, `classeTemporelle`
(HPH, HCH, HPE, HCE…), `rang`, `calFou` / `libelleCalFou`, `quantiteConsommationKWH`, `montantHT`,
`numeroFacture`, `dateFacture`. Response `details.hasError` = partial result. De-duplicate by
(invoice, point, start, end, class, rank, calendar).

## Sites / contracts — `sites[].site`

`pod`, `energie`, `segment` (C4, T3…), `designation`, `isCommunicant`, `idCompte`, `idEnseigne`,
`situationContractuelle.{numeroContrat, statutContrat, dateDebutContrat, dateResiliation (9999-12-31 =
none), isActif, isResilie, isEnCoursDeResiliation, fta, optionTarifaire, puissancePointe,
puissanceHeuresPleinesHiver, puissanceHeuresCreusesHiver, puissanceHeuresPleinesEte,
puissanceHeuresCreusesEte, puissanceSouscrite, poucentageEnergieVerte}`,
`situationComptage.{matriculeCompteur, frequenceReleve}`.

## Live validation of the HA client (21/11/2026, `scripts/live_check.py`)

- `/web-methods/factures` **without** the trailing `?` works (aiohttp): 24 invoices over 12 months.
- Login: id_token valid 60 min, refresh token returned.
- `details.hasError` is `true` on a normal consumption search (the MCP connector gets the same, with
  the same lines): record it, never treat it as a failed fetch.
- `rang` and `calFou` / `libelleCalFou` come back empty in practice. Gas lines have no
  `classeTemporelle`. One electricity invoice can carry two periods split at the 1st of January.
- `statutContrat` `45` = active, `60` = terminated (with `dateResiliation` set).

## Known gaps

- No contract documents or prices via the API ("documents contractuels" has no backend); unit prices
  only on invoice PDFs. Contract end date not exposed.
- `puissances/*` routes seen in the front answer 404 for this account; use the PDF or the distributor.
