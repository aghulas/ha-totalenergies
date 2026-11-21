"""Pure functions and data classes: TotalEnergies payloads → typed rows, search windows.

Same semantics as the MCP connector's `modeles.py` (normaliser_facture, normaliser_ligne_conso,
fenetres) and `detail.py` (normaliser_site). Tolerant: a missing or malformed field becomes None,
never an exception — the API is undocumented.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

Energy = Literal["electricity", "gas"]
ENERGIES: dict[str, Energy] = {"01": "electricity", "02": "gas"}

# situationContractuelle.statutContrat → status (seen on the portal; unknown codes fall back on
# the isActif / isResilie flags).
CONTRACT_STATUSES: dict[str, str] = {
    "45": "active",
    "50": "active",
    "55": "changing",
    "60": "terminated",
    "65": "terminated",
    "70": "terminated",
}

# situationContractuelle field → time class of the subscribed power (kVA).
SUBSCRIBED_POWER_FIELDS: dict[str, str] = {
    "peak": "puissancePointe",
    "hph": "puissanceHeuresPleinesHiver",
    "hch": "puissanceHeuresCreusesHiver",
    "hpe": "puissanceHeuresPleinesEte",
    "hce": "puissanceHeuresCreusesEte",
    "single": "puissanceSouscrite",
}


# ---- scalar parsing -------------------------------------------------------------------------


def parse_number(value: Any) -> float | None:
    """A float from a number or a string with a comma or dot decimal ("1 234,56" too)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.replace(" ", "").replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    """A date from "YYYY-MM-DD…"; "0000-…" (the API's empty date) and "9999-12-31" → None."""
    if not isinstance(value, str) or value.startswith(("0000", "9999")):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _str(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    text = _str(value)
    return int(text) if text is not None and text.isdigit() else None


def _energy(value: Any) -> Energy | None:
    return ENERGIES.get(value) if isinstance(value, str) else None


def _unwrap(item: Any, key: str) -> dict[str, Any]:
    """The API wraps some rows (`{"facture": {...}}`), not always."""
    if not isinstance(item, dict):
        return {}
    inner = item.get(key)
    return inner if isinstance(inner, dict) else item


def _rows(payload: Any, key: str) -> list[Any]:
    rows = payload.get(key) if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) else []


# ---- search windows -------------------------------------------------------------------------


def windows(start: date, end: date, months: int = 12) -> list[tuple[date, date]]:
    """Split [start, end] (inclusive) into consecutive windows of at most `months` months."""
    if end < start:
        raise ValueError("end before start")
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        years, month0 = divmod(cur.month - 1 + months, 12)
        year, month = cur.year + years, month0 + 1
        nxt = date(year, month, min(cur.day, calendar.monthrange(year, month)[1]))
        stop = min(end, nxt - timedelta(days=1))
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out


# ---- accounts -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Account:
    """A billing account (`/web-methods/comptes`)."""

    number: str | None
    name: str | None
    segment: str | None
    invoice_delivery: str | None  # modeEnvoiFacture


def parse_accounts(payload: Any) -> list[Account]:
    out = []
    for item in _rows(payload, "comptes"):
        c = _unwrap(item, "compte")
        out.append(
            Account(
                number=_str(c.get("numero")),
                name=_str(c.get("nom")),
                segment=_str(c.get("segment")),
                invoice_delivery=_str(c.get("modeEnvoiFacture")),
            )
        )
    return out


# ---- sites / contracts ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Contract:
    """`situationContractuelle` of a delivery point. No price and no end date in the API."""

    number: str | None
    status: str  # active | changing | terminating | terminated | unknown
    status_code: str | None
    start: date | None
    termination: date | None  # None: open-ended (9999-12-31)
    active: bool
    tariff_formula: str | None  # FTA (formule tarifaire d'acheminement)
    tariff_option: str | None
    subscribed_power_kva: dict[str, float]  # keys of SUBSCRIBED_POWER_FIELDS
    green_energy_pct: float | None


@dataclass(frozen=True, slots=True)
class Site:
    """A delivery point (PRM for electricity, PCE for gas) and its contract."""

    point: str | None
    energy: Energy | None
    segment: str | None  # C4, T3…
    designation: str | None
    communicating: bool | None
    account: str | None  # idCompte
    brand_id: str | None  # idEnseigne
    contract: Contract
    meter: str | None  # matriculeCompteur
    reading_frequency: str | None


def _contract_status(sc: dict[str, Any]) -> tuple[str, str | None]:
    code = _str(sc.get("statutContrat"))
    if sc.get("isResilie") is True:
        return "terminated", code
    if sc.get("isEnCoursDeResiliation") is True:
        return "terminating", code
    if code in CONTRACT_STATUSES:
        return CONTRACT_STATUSES[code], code
    return ("active" if sc.get("isActif") is True else "unknown"), code


def parse_site(item: Any) -> Site:
    s = _unwrap(item, "site")
    sc = s.get("situationContractuelle")
    sc = sc if isinstance(sc, dict) else {}
    meter = s.get("situationComptage")
    meter = meter if isinstance(meter, dict) else {}
    powers = {
        key: kva
        for key, field in SUBSCRIBED_POWER_FIELDS.items()
        if (kva := parse_number(sc.get(field))) is not None
    }
    status, code = _contract_status(sc)
    return Site(
        point=_str(s.get("pod")),
        energy=_energy(s.get("energie")),
        segment=_str(s.get("segment")),
        designation=_str(s.get("designation")),
        communicating=_bool(s.get("isCommunicant")),
        account=_str(s.get("idCompte")),
        brand_id=_str(s.get("idEnseigne")),
        contract=Contract(
            number=_str(sc.get("numeroContrat")),
            status=status,
            status_code=code,
            start=parse_date(sc.get("dateDebutContrat")),
            termination=parse_date(sc.get("dateResiliation")),
            active=sc.get("isActif") is True,
            tariff_formula=_str(sc.get("fta")),
            tariff_option=_str(sc.get("optionTarifaire")),
            subscribed_power_kva=powers,
            green_energy_pct=parse_number(sc.get("poucentageEnergieVerte")),  # sic
        ),
        meter=_str(meter.get("matriculeCompteur")),
        reading_frequency=_str(meter.get("frequenceReleve")),
    )


def parse_sites(payload: Any) -> list[Site]:
    return [parse_site(item) for item in _rows(payload, "sites")]


# ---- invoices -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Invoice:
    """An invoice (`/web-methods/factures?`). Amounts in EUR, energy in kWh.

    The invoice date is its issue date; the consumption it bills is earlier (see the
    consumption lines for the periods).
    """

    number: str | None
    date: date | None
    energy: Energy | None
    type: str | None
    account_contract: str | None  # descriptifCompteContrat
    contract: str | None  # descriptifContrat
    brand_id: str | None
    points: int | None  # nbPOD
    energy_kwh: float | None  # consoTotalKWH
    subtotal_excl_tax: float | None  # montantSousTotalHT
    taxes_excl_vat: float | None  # sommeTaxes
    total_excl_vat: float | None  # montantTotalHorsTVA
    vat: float | None  # montantTVA
    total_incl_vat: float | None  # montantTTC
    printed_on: dt.date | None  # dateImpression
    cancellation_invoice: str | None  # numeroFactureAnnulation
    cancellation_printed_on: dt.date | None  # dateImpressionAnnulation
    delivery: str | None  # modeEnvoi
    chorus_status: str | None

    @property
    def cancelled(self) -> bool:
        return self.cancellation_invoice is not None or self.cancellation_printed_on is not None


def parse_invoice(item: Any) -> Invoice:
    f = _unwrap(item, "facture")
    return Invoice(
        number=_str(f.get("numero")),
        date=parse_date(f.get("date")),
        energy=_energy(f.get("secteurActivite")),
        type=_str(f.get("type")),
        account_contract=_str(f.get("descriptifCompteContrat")),
        contract=_str(f.get("descriptifContrat")),
        brand_id=_str(f.get("idEnseigne")),
        points=_int(f.get("nbPOD")),
        energy_kwh=parse_number(f.get("consoTotalKWH")),
        subtotal_excl_tax=parse_number(f.get("montantSousTotalHT")),
        taxes_excl_vat=parse_number(f.get("sommeTaxes")),
        total_excl_vat=parse_number(f.get("montantTotalHorsTVA")),
        vat=parse_number(f.get("montantTVA")),
        total_incl_vat=parse_number(f.get("montantTTC")),
        printed_on=parse_date(f.get("dateImpression")),
        cancellation_invoice=_str(f.get("numeroFactureAnnulation")),
        cancellation_printed_on=parse_date(f.get("dateImpressionAnnulation")),
        delivery=_str(f.get("modeEnvoi")),
        chorus_status=_str(f.get("statutChorus")),
    )


@dataclass(frozen=True, slots=True)
class InvoiceSearch:
    """Invoices of a period, de-duplicated by number, most recent first."""

    invoices: list[Invoice]
    heavy_load: bool  # isHeavyLoad on any window


def parse_invoices(payload: Any) -> tuple[list[Invoice], bool]:
    heavy = isinstance(payload, dict) and payload.get("isHeavyLoad") is True
    return [parse_invoice(item) for item in _rows(payload, "factures")], heavy


def merge_invoices(batches: list[list[Invoice]], heavy_load: bool) -> InvoiceSearch:
    seen: dict[str, Invoice] = {}
    for batch in batches:
        for inv in batch:
            if inv.number:
                seen[inv.number] = inv
    ordered = sorted(
        seen.values(), key=lambda i: (i.date or date.min, i.number or ""), reverse=True
    )
    return InvoiceSearch(ordered, heavy_load)


# ---- consumption lines ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConsumptionLine:
    """A billed consumption line: one invoice, one point, one period, one time class."""

    point: str | None  # pod (PRM / PCE)
    energy: Energy | None
    start: date | None
    end: date | None
    time_class: str | None  # classeTemporelle: HPH, HCH, HPE, HCE… (gas: often empty)
    rank: str | None
    supplier_calendar: str | None  # libelleCalFou or calFou
    energy_kwh: float | None
    amount_excl_tax: float | None  # montantHT
    invoice: str | None
    invoice_date: date | None

    @property
    def key(self) -> tuple[str | None, ...]:
        """Identity of the line across overlapping searches."""
        return (
            self.invoice,
            self.point,
            self.start.isoformat() if self.start else None,
            self.end.isoformat() if self.end else None,
            self.time_class,
            self.rank,
            self.supplier_calendar,
        )


def parse_consumption_line(item: Any) -> ConsumptionLine:
    c = item if isinstance(item, dict) else {}
    return ConsumptionLine(
        point=_str(c.get("pod")),
        energy=_energy(c.get("energie")),
        start=parse_date(c.get("dateDebutPeriode")),
        end=parse_date(c.get("dateFinPeriode")),
        time_class=_str(c.get("classeTemporelle")),
        rank=_str(c.get("rang")),
        supplier_calendar=_str(c.get("libelleCalFou")) or _str(c.get("calFou")),
        energy_kwh=parse_number(c.get("quantiteConsommationKWH")),
        amount_excl_tax=parse_number(c.get("montantHT")),
        invoice=_str(c.get("numeroFacture")),
        invoice_date=parse_date(c.get("dateFacture")),
    )


@dataclass(frozen=True, slots=True)
class ConsumptionSearch:
    """Billed consumption lines of a period, de-duplicated, ordered by start, point, class."""

    lines: list[ConsumptionLine]
    partial: bool  # details.hasError on any window: some lines may be missing


def parse_consumptions(payload: Any) -> tuple[list[ConsumptionLine], bool]:
    details = payload.get("details") if isinstance(payload, dict) else None
    partial = isinstance(details, dict) and details.get("hasError") is True
    return [parse_consumption_line(item) for item in _rows(payload, "consumptions")], partial


def merge_consumptions(batches: list[list[ConsumptionLine]], partial: bool) -> ConsumptionSearch:
    seen: dict[tuple[str | None, ...], ConsumptionLine] = {}
    for batch in batches:
        for line in batch:
            seen[line.key] = line
    ordered = sorted(
        seen.values(),
        key=lambda x: (x.start or date.min, x.point or "", x.time_class or "", x.rank or ""),
    )
    return ConsumptionSearch(ordered, partial)
