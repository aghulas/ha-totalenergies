# TotalEnergies Pro — Home Assistant integration

Unofficial integration, not affiliated with TotalEnergies. *[Version française plus bas.](#français)*

Brings the data of TotalEnergies **business** customers (pro.totalenergies.fr) into Home Assistant,
read-only: invoices, billed electricity and gas per delivery point and time class, **billed costs** as
long-term statistics, effective prices per kWh, and contract facts.

It complements the integrations of the distributors, which measure the quantities (Enedis for
electricity, GRDF for gas) but know nothing about money. TotalEnergies only knows billing periods, so
this integration does not replace them: it puts the cost of each invoice on the days it bills, and the
Energy dashboard shows those costs next to the distributor's consumption.

**Status.** It runs on one real Home Assistant since November 2026: three years of invoices imported,
totals checked to the cent against TotalEnergies. Versions: see the [changelog](CHANGELOG.md).

## Why this design

- **Never lock the account.** A password refused by TotalEnergies is never sent again: the integration
  stops contacting TotalEnergies, raises a repair issue and asks you to re-authenticate. Between
  fetches it uses the session TotalEnergies grants (renewed with its refresh token), not the password.
- **Never fight the anti-bot protection.** If TotalEnergies' API answers with its anti-bot block, the
  integration waits a day, and raises a repair issue if it happens several days in a row. It never
  retries in a loop and never tries to work around it.
- **Poll little.** Invoices are monthly: one fetch a day, at least one second between requests, searches
  in 12-month windows.
- **Read-only.** Only reading routes are allowed (an allow-list in the code): no payment, request or
  account change.

This behaviour is covered by automated tests against a local fake of the API; it has deliberately not
been provoked on a real account.

## Installation

Requires Home Assistant 2025.11 or later (the statistics use the unit class introduced in that
version). The icon shipped with the integration is shown from Home Assistant 2026.3.

**Home Assistant 2026.11 or later — Marketplace:** Settings → Marketplace → **Add from link**, enter
`aghulas/ha-totalenergies`, then open the repository and select **Install**.

**Earlier versions — HACS:** HACS → Integrations → menu → *Custom repositories*, add
`https://github.com/aghulas/ha-totalenergies` as an *Integration*, install it, then restart Home
Assistant.

**Manually:** copy `custom_components/totalenergies_pro` into the `custom_components` folder of your
configuration, then restart Home Assistant.

Then: Settings → Devices & services → Add integration → **TotalEnergies Pro**, enter the e-mail and
password of your pro.totalenergies.fr account, and pick the delivery points (active ones are
preselected). Check first that the password works on pro.totalenergies.fr: it is tried once.

On the first setup, the history is imported in one go (a handful of requests: three years take under a
minute).

## What you get

**Statistics** (per delivery point, external statistics `totalenergies_pro:<point>_…`, one value per day):

| Statistic | Content |
|---|---|
| `billed_energy` (kWh), `billed_energy_<class>` | billed consumption, total and per time class (HPH, HCH, HPE, HCE, peak…; gas has none) |
| `energy_cost` (€ excl. tax) | the energy part of the consumption lines |
| `cost_excl_vat`, `cost_incl_vat` (€) | the whole invoice: energy, subscription, network, taxes |

Each amount is placed on the days of the period it bills, not on the invoice date (or, as an option, on
the last day of the period). Cancelled invoices and their lines count for nothing; credit notes offset
naturally. Re-importing is idempotent: corrections by TotalEnergies replace the old values.

**Energy dashboard.** Keep the distributor's statistic as the source and use the TotalEnergies **cost**
as its cost: Settings → Dashboards → Energy → edit the electricity (Enedis) or gas (GRDF) source →
*Use an entity tracking the total costs* → `TotalEnergies … cost (incl. VAT)` (or excl. VAT if you
recover VAT). The distributors' statistics are external, and Home Assistant accepts only a cost
statistic on them — not an entity with the current price nor a fixed price.

Costs appear as invoices arrive: the days not billed yet (usually the last few weeks) show no cost until
the next invoice. Do not add the `billed_energy` statistics as a source next to the distributor's: the
energy would be counted twice.

**Entities** (one device per delivery point):

- last invoice (incl. VAT, with period, kWh and amounts as attributes), its date and its kWh;
- average price per kWh over 12 months, excl. and incl. VAT — all-in, for cards and automations;
- energy price (energy part of the last invoice, per time class as attributes);
- invoiced over 12 months;
- contract status, start, network tariff and subscribed power (electricity);
- read from the invoice PDFs: the **price of one more kWh** (excl. VAT: supply, capacity, energy
  savings certificates, network variable part and excise — per time class as attributes), the
  **maximum power reached** over 12 months (electricity, per class, with the subscribed power and the
  overrun hours) and the gas **annual reference consumption** (CAR, with the network tariff, profile and
  conversion factor);
- a *connection problem* sensor (with the last error and its date) and a *refresh* button (at most one
  fetch every five minutes).

**Service** `totalenergies_pro.download_invoice`: saves invoice PDFs to the folder set in the options
(off by default; the folder must be in `allowlist_external_dirs`). By default, the last invoice of each
point. Existing files are never overwritten.

## Options

| Option | Default | Notes |
|---|---|---|
| History to import | 3 years | 1 to 10. Raising it imports the older invoices at the next fetch. |
| Daily fetch hour | 11 | Local time; a random delay of up to 30 minutes is added. Invoices are issued around the 10th. |
| Where billed amounts are placed | spread over the days of the period | or on the last day of the period. Changing it re-imports everything. |
| Folder for invoice PDFs | empty (off) | e.g. `/media/totalenergies` |
| Read the invoice PDFs | on | One download per invoice, ever: the last 12 months at first, a few a day, then each new invoice. |

## Limits

- The data is the **billing** view: monthly, possibly estimated, issued weeks after the consumption. For
  the measurement, use the distributor's integration.
- A subscription billed in advance ("à échoir") and taken back later can move part of the fixed charges
  from one month to the next; yearly totals are exact.
- TotalEnergies sometimes withholds the list of delivery points for a while. The setup then offers the
  points billed in the last 12 months; the tariff and the subscribed power are then taken from the
  invoice PDFs, the other contract sensors stay unknown until the list comes back. Invoices and costs
  are not affected.
- The invoice PDFs are read by their layout: a change of the invoice template could stop that reading
  (the sensors from the PDFs then stop updating; nothing else is affected).
- The API is the one of the customer portal: unofficial, undocumented, subject to change.

## If TotalEnergies refuses the sign-in

The integration stops and a repair issue appears. Sign in on pro.totalenergies.fr in a browser (reset
the password if needed), stop any other tool still using an old password, then re-authenticate the
integration from Settings → Devices & services. The new password is tried once.

## Privacy

Your password is stored by Home Assistant like any other integration credential; the session tokens are
kept in the integration's private storage. Delivery point numbers are used as identifiers of the
statistics and entities. Downloaded diagnostics contain neither the password, the tokens, the e-mail,
nor any account, contract, point or invoice number, name or address.

## Development

See `CLAUDE.md`, `docs/requirements.md` and `docs/api.md`. `scripts/live_check.py` and
`scripts/sites_probe.py` are supervised one-shot checks against the real account (one sign-in, report
without personal data).

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && mypy custom_components/totalenergies_pro && pytest
```

License: MIT. The icon is original artwork, not TotalEnergies' logo.

---

## Français

Intégration non officielle, sans lien avec TotalEnergies.

Elle apporte dans Home Assistant, en lecture seule, les données des clients **professionnels** de
TotalEnergies (pro.totalenergies.fr) : factures, électricité et gaz facturés par point de livraison et
par poste, **coûts facturés** en statistiques à long terme, prix effectifs au kWh et informations de
contrat.

Elle complète les intégrations des distributeurs, qui mesurent les quantités (Enedis pour l'électricité,
GRDF pour le gaz) mais ne connaissent pas les montants. TotalEnergies ne connaît que des périodes de
facturation : l'intégration ne les remplace donc pas, elle place le coût de chaque facture sur les jours
qu'elle facture, et le tableau de bord Énergie l'affiche à côté de la consommation du distributeur.

**État.** Elle tourne sur un vrai Home Assistant depuis novembre 2026 : trois ans de factures importés,
totaux recoupés au centime avec TotalEnergies. Versions : voir le [journal des versions](CHANGELOG.md).

### Pourquoi cette conception

- **Ne jamais verrouiller le compte.** Un mot de passe refusé par TotalEnergies n'est jamais renvoyé :
  l'intégration cesse de contacter TotalEnergies, crée une alerte dans Réparations et demande de se
  ré-authentifier. Entre deux collectes, elle utilise la session accordée par TotalEnergies
  (renouvelée par son jeton de rafraîchissement), pas le mot de passe.
- **Ne jamais forcer la protection anti-robot.** Si l'API de TotalEnergies répond par son blocage
  anti-robot, l'intégration attend le lendemain, et crée une alerte si cela se répète plusieurs jours.
  Elle ne réessaie jamais en boucle et ne cherche jamais à le contourner.
- **Interroger peu.** Les factures sont mensuelles : une collecte par jour, au moins une seconde entre
  deux requêtes, recherches par fenêtres de 12 mois.
- **Lecture seule.** Seules les routes de lecture sont permises (liste blanche dans le code) : aucun
  paiement, aucune demande, aucune modification du compte.

Ce comportement est couvert par des tests automatiques sur un faux serveur ; il n'a volontairement pas
été provoqué sur un vrai compte.

### Installation

Home Assistant 2025.11 ou plus récent (les statistiques utilisent la classe d'unité apparue dans cette
version). L'icône fournie avec l'intégration s'affiche à partir de Home Assistant 2026.3.

**Home Assistant 2026.11 ou plus récent, par le Marketplace :** Paramètres → Marketplace → **Add from
link**, saisir `aghulas/ha-totalenergies`, puis ouvrir le dépôt et choisir **Install**.

**Versions antérieures, par HACS :** HACS → Intégrations → menu → *Dépôts personnalisés*, ajouter
`https://github.com/aghulas/ha-totalenergies` comme *Intégration*, l'installer, puis redémarrer Home
Assistant.

**À la main :** copier `custom_components/totalenergies_pro` dans le dossier `custom_components` de la
configuration, puis redémarrer Home Assistant.

Ensuite : Paramètres → Appareils et services → Ajouter une intégration → **TotalEnergies Pro**, saisir
l'e-mail et le mot de passe du compte pro.totalenergies.fr, et choisir les points de livraison (les
actifs sont cochés). Vérifiez d'abord que ce mot de passe fonctionne sur pro.totalenergies.fr : il est
essayé une seule fois.

À la première installation, l'historique est importé d'un coup (quelques requêtes : trois ans prennent
moins d'une minute).

### Ce que vous obtenez

**Statistiques** (par point de livraison, statistiques externes `totalenergies_pro:<point>_…`, une valeur
par jour) :

| Statistique | Contenu |
|---|---|
| `billed_energy` (kWh), `billed_energy_<poste>` | consommation facturée, totale et par poste (HPH, HCH, HPE, HCE, pointe… ; le gaz n'en a pas) |
| `energy_cost` (€ HT) | la part énergie des lignes de consommation |
| `cost_excl_vat`, `cost_incl_vat` (€) | la facture entière : énergie, abonnement, acheminement, taxes |

Chaque montant est placé sur les jours de la période qu'il facture, pas à la date de la facture (ou, en
option, sur le dernier jour de la période). Les factures annulées et leurs lignes ne comptent pas ; les
avoirs se compensent d'eux-mêmes. Le réimport est idempotent : une correction de TotalEnergies remplace
les anciennes valeurs.

**Tableau de bord Énergie.** Gardez la statistique du distributeur comme source et prenez le **coût**
TotalEnergies comme coût : Paramètres → Tableaux de bord → Énergie → modifier la source électricité
(Enedis) ou gaz (GRDF) → *Utiliser une entité qui suit les coûts totaux* → `TotalEnergies … cost (incl.
VAT)` (ou HT si vous récupérez la TVA). Les statistiques des distributeurs sont externes, et Home
Assistant n'y accepte qu'une statistique de coût, ni entité de prix actuel ni prix fixe.

Les coûts apparaissent au fil des factures : les jours pas encore facturés (en général les dernières
semaines) n'ont pas de coût avant la facture suivante. N'ajoutez pas les statistiques `billed_energy`
comme source à côté de celle du distributeur : l'énergie serait comptée deux fois.

**Entités** (un appareil par point de livraison) :

- dernière facture (TTC, avec période, kWh et montants en attributs), sa date et ses kWh ;
- prix moyen au kWh sur 12 mois, HT et TTC, tout compris, pour les cartes et les automatisations ;
- prix de l'énergie (part énergie de la dernière facture, par poste en attributs) ;
- facturé sur 12 mois ;
- statut, début, tarif d'acheminement et puissance souscrite du contrat (électricité) ;
- lus sur les PDF des factures : le **prix d'un kWh de plus** (HT : fourniture, capacité, certificats
  d'économie d'énergie, part variable de l'acheminement et accise, par poste en attributs), la
  **puissance maximale atteinte** sur 12 mois (électricité, par poste, avec la puissance souscrite et
  les heures de dépassement) et la **consommation annuelle de référence** du gaz (CAR, avec le tarif
  d'acheminement, le profil et le coefficient de conversion) ;
- un capteur *problème de connexion* (avec la dernière erreur et sa date) et un bouton *actualiser*
  (une collecte toutes les cinq minutes au plus).

**Action** `totalenergies_pro.download_invoice` : enregistre les PDF des factures dans le dossier choisi
dans les options (désactivé par défaut ; le dossier doit figurer dans `allowlist_external_dirs`). Par
défaut, la dernière facture de chaque point. Un fichier existant n'est jamais écrasé.

### Options

| Option | Défaut | Remarques |
|---|---|---|
| Historique à importer | 3 ans | 1 à 10. L'augmenter importe les factures plus anciennes à la collecte suivante. |
| Heure de la collecte quotidienne | 11 | Heure locale ; un délai aléatoire de 30 minutes au plus s'ajoute. Les factures sont émises vers le 10. |
| Placement des montants facturés | répartis sur les jours de la période | ou sur le dernier jour de la période. Le changer réimporte tout. |
| Dossier des PDF de factures | vide (désactivé) | par exemple `/media/totalenergies` |
| Lire les PDF des factures | oui | Un seul téléchargement par facture : les 12 derniers mois au début, quelques-uns par jour, puis chaque nouvelle facture. |

### Limites

- Les données sont la vue **facturation** : mensuelle, parfois estimée, émise des semaines après la
  consommation. Pour la mesure, utilisez l'intégration du distributeur.
- Un abonnement facturé « à échoir » puis repris peut décaler une partie des frais fixes d'un mois sur
  l'autre ; les totaux annuels sont exacts.
- TotalEnergies retient parfois la liste des points de livraison pendant un temps. L'installation propose
  alors les points facturés ces 12 derniers mois ; le tarif et la puissance souscrite sont alors lus sur
  les PDF des factures, les autres capteurs de contrat restent inconnus jusqu'à son retour. Les factures
  et les coûts n'en souffrent pas.
- Les PDF des factures sont lus d'après leur mise en page : un changement de modèle de facture peut
  interrompre cette lecture (les capteurs tirés des PDF cessent alors d'être mis à jour, rien d'autre).
- L'API est celle de l'espace client : non officielle, non documentée, susceptible de changer.

### Si TotalEnergies refuse la connexion

L'intégration s'arrête et une alerte apparaît dans Réparations. Connectez-vous sur pro.totalenergies.fr
dans un navigateur (changez le mot de passe si besoin), arrêtez tout autre outil qui utiliserait encore
un ancien mot de passe, puis ré-authentifiez l'intégration depuis Paramètres → Appareils et services. Le
nouveau mot de passe est essayé une seule fois.

### Vie privée

Votre mot de passe est conservé par Home Assistant comme pour toute intégration ; les jetons de session
sont gardés dans le stockage privé de l'intégration. Les numéros de point de livraison servent
d'identifiants aux statistiques et aux entités. Les diagnostics téléchargés ne contiennent ni le mot de
passe, ni les jetons, ni l'e-mail, ni aucun numéro de compte, de contrat, de point ou de facture, nom ou
adresse.

Licence : MIT. L'icône est une création originale, pas le logo de TotalEnergies.
