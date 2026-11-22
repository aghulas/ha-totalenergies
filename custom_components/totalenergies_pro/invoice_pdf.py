"""Reading TotalEnergies invoice PDFs: unit prices, subscribed / reached power, gas references.

Pure Python (pypdf in layout mode), no Home Assistant dependency. The rules are those of the MCP
connector (`detail.py`, written for `pdftotext -layout`); on 26 real invoices the two extractions
give identical results. What is kept per invoice is a small summary (`InvoiceDetail`): no number,
name or address leaves this module.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Time classes as printed on the invoices → keys used by the integration (same as the API's).
CLASSES: dict[str, str] = {
    "Pointe": "peak",
    "Heures Pleines Hiver": "hph",
    "Heures Creuses Hiver": "hch",
    "Heures Pleines Eté": "hpe",
    "Heures Creuses Eté": "hce",
}
POWER_ROWS: dict[str, str] = {**CLASSES, "Puissance pondérée": "weighted"}
GAS_CONSUMPTION = "Consommation"

_NB = r"(?:-?\d{1,3}(?:[  ]\d{3})+(?:,\d+)?|-?\d+(?:,\d+)?)"  # « 4 467 », « 1301,40000 », « -30 »
_LINE = re.compile(
    r"^\s*(?P<lib>\S.*?)\s{2,}(?P<du>\d\d/\d\d/\d\d)\s+(?P<au>\d\d/\d\d/\d\d)(?P<rest>.*)$"
)
_QTY = re.compile(rf"({_NB})\s*(kWhc|kWh|m3|jour|kW\.j|kVA)\b")
_PRICE = re.compile(rf"({_NB})\s*€/([A-Za-z.]+)")
_AMOUNT = re.compile(rf"({_NB})\s*€(?:\s+(\d+)\s*%)?\s*$")
_SECTION = re.compile(rf"^\s*(?P<lib>[A-ZÉ][^\d€]{{3,80}}?)\s{{2,}}(?P<mt>{_NB})\s*€\s*$")
_REFERENCES: dict[str, re.Pattern[str]] = {
    "offer": re.compile(r"(Offre [^\n]{3,40}?)(?:\s{2,}|$)", re.MULTILINE),
    "network_tariff": re.compile(r"Tarif d'acheminement\s*:\s*(\S+)"),
    "zone": re.compile(r"Zone Tarifaire\s*:\s*(\S+)"),
    "profile": re.compile(r"Profil\s*:\s*(\S+)"),
    "regional_level": re.compile(r"Niveau Tarifaire Régional\s*:\s*(\S+)"),
    "car_kwh": re.compile(rf"Consommation Annuelle de Référence\s*:\s*({_NB})\s*kWh"),
    "conversion_factor": re.compile(rf"Coef\. de conversion \(m3 à kWh\)\s*:\s*({_NB})"),
    "tariff_option": re.compile(r"Option tarifaire\s*:\s*([^\n]+?)(?:\s{2,}|$)", re.MULTILINE),
    "segment": re.compile(r"Segment\s*:\s*(\S+)"),  # electricity: C4, C5…
    "connection": re.compile(r"Raccordement\s*:\s*([^\n]+?)(?:\s{2,}|$)", re.MULTILINE),
    "invoice_date": re.compile(r"Date de facture\s*:\s*(\d\d/\d\d/\d\d)"),
}
_NUMERIC = ("car_kwh", "conversion_factor")


def number(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _day(text: str) -> dt.date | None:
    try:
        day, month, year = text.split("/")
        return dt.date(2000 + int(year), int(month), int(day))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Line:
    """One billing line of the PDF."""

    section: str | None
    label: str
    start: dt.date | None
    end: dt.date | None
    quantity: float | None
    unit: str | None
    unit_price: float | None
    price_unit: str | None
    amount_excl_tax: float | None


@dataclass(frozen=True, slots=True)
class Parsed:
    references: dict[str, Any]
    lines: list[Line]
    subscribed_kva: dict[str, float]
    reached_kva: dict[str, float]
    overrun_hours: dict[str, float]


def pdf_text(data: bytes) -> str:
    """The text of a PDF with its layout kept (columns separated by runs of spaces). Blocking."""
    from pypdf import PdfReader  # noqa: PLC0415 — heavy import, only where a PDF is read

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font warnings, harmless here
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text(extraction_mode="layout") for page in reader.pages)


def _powers(
    rows: list[str], start: str, ends: tuple[str, ...]
) -> dict[str, tuple[float, float | None]]:
    out: dict[str, tuple[float, float | None]] = {}
    inside = False
    for row in rows:
        if start in row:
            inside = True
            continue
        if inside and any(e in row for e in ends):
            break
        if inside:
            for label, key in POWER_ROWS.items():
                m = re.match(rf"^\s*{label}\s+(\d+)(?:\s+(\d+))?", row)
                if m:
                    out[key] = (float(m.group(1)), float(m.group(2)) if m.group(2) else None)
    return out


def parse_text(text: str) -> Parsed:
    rows = text.splitlines()
    refs: dict[str, Any] = {}
    for key, rx in _REFERENCES.items():
        m = rx.search(text)
        if m:
            value = m.group(1).strip()
            if key in _NUMERIC:
                refs[key] = number(value)
            elif key == "invoice_date":
                refs[key] = _day(value)
            else:
                refs[key] = value
    section: str | None = None
    lines: list[Line] = []
    for row in rows:
        ms = _SECTION.match(row)
        if ms:
            section = ms.group("lib").replace("(HT)", "").strip()
            continue
        m = _LINE.match(row)
        if not m or "€" not in m.group("rest"):
            continue
        rest = m.group("rest")
        price = _PRICE.search(rest)
        amount = _AMOUNT.search(rest)
        qtys = _QTY.findall(rest[: price.start()] if price else rest)
        q = qtys[-1] if qtys else None
        lines.append(
            Line(
                section=section,
                label=re.sub(r"\s+", " ", m.group("lib")).strip(),
                start=_day(m.group("du")),
                end=_day(m.group("au")),
                quantity=number(q[0]) if q else None,
                unit=q[1] if q else None,
                unit_price=number(price.group(1)) if price else None,
                price_unit=price.group(2) if price else None,
                amount_excl_tax=number(amount.group(1)) if amount else None,
            )
        )
    subscribed = _powers(
        rows, "Puissances souscrites (kVA)", ("Historique", "Relevé de consommation")
    )
    reached = _powers(rows, "Puissances mesurées", ("Quantité", "Prix unitaire", "Energie active"))
    return Parsed(
        references=refs,
        lines=lines,
        subscribed_kva={k: v for k, (v, _) in subscribed.items()},
        reached_kva={k: v for k, (v, _) in reached.items() if k != "weighted"},
        overrun_hours={k: h for k, (_, h) in reached.items() if h is not None and k != "weighted"},
    )


def _supply_class(label: str) -> str | None:
    """Time class of a supply line ("Heures Pleines Eté", or "Consommation Heures Pleines Eté" since
    the invoices of November 2026); "base" for the gas consumption line; None for other lines."""
    if label == GAS_CONSUMPTION:
        return "base"
    name = label.removeprefix(f"{GAS_CONSUMPTION} ")
    return CLASSES.get(name)


def _class_of(label: str) -> str | None:
    for name in sorted(CLASSES, key=len, reverse=True):
        if name in label:
            return CLASSES[name]
    return None


@dataclass(frozen=True, slots=True)
class InvoiceDetail:
    """What the integration keeps of one invoice PDF."""

    invoice_date: dt.date | None = None
    period_start: dt.date | None = None
    period_end: dt.date | None = None
    kwh: float | None = None
    # Price of one more kWh excl. VAT: supply, capacity, energy savings certificates, network
    # variable part and excise — per time class, and kWh-weighted over the invoice.
    marginal_price: float | None = None
    marginal_price_by_class: dict[str, float] = field(default_factory=dict)
    subscribed_kva: dict[str, float] = field(default_factory=dict)
    reached_kva: dict[str, float] = field(default_factory=dict)
    overrun_hours: dict[str, float] = field(default_factory=dict)
    references: dict[str, Any] = field(default_factory=dict)  # offer, network tariff, CAR…

    def as_dict(self) -> dict[str, Any]:
        def iso(d: dt.date | None) -> str | None:
            return d.isoformat() if d else None

        return {
            "invoice_date": iso(self.invoice_date),
            "period_start": iso(self.period_start),
            "period_end": iso(self.period_end),
            "kwh": self.kwh,
            "marginal_price": self.marginal_price,
            "marginal_price_by_class": self.marginal_price_by_class,
            "subscribed_kva": self.subscribed_kva,
            "reached_kva": self.reached_kva,
            "overrun_hours": self.overrun_hours,
            "references": {
                k: (v.isoformat() if isinstance(v, dt.date) else v)
                for k, v in self.references.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Any) -> InvoiceDetail | None:
        if not isinstance(data, dict):
            return None

        def day(v: Any) -> dt.date | None:
            try:
                return dt.date.fromisoformat(v) if isinstance(v, str) else None
            except ValueError:
                return None

        def floats(v: Any) -> dict[str, float]:
            if not isinstance(v, dict):
                return {}
            return {str(k): float(x) for k, x in v.items() if isinstance(x, int | float)}

        def opt(v: Any) -> float | None:
            return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

        refs = data.get("references")
        return cls(
            invoice_date=day(data.get("invoice_date")),
            period_start=day(data.get("period_start")),
            period_end=day(data.get("period_end")),
            kwh=opt(data.get("kwh")),
            marginal_price=opt(data.get("marginal_price")),
            marginal_price_by_class=floats(data.get("marginal_price_by_class")),
            subscribed_kva=floats(data.get("subscribed_kva")),
            reached_kva=floats(data.get("reached_kva")),
            overrun_hours=floats(data.get("overrun_hours")),
            references=dict(refs) if isinstance(refs, dict) else {},
        )


def summarize_invoice(parsed: Parsed) -> InvoiceDetail:
    """Marginal prices and power of one invoice.

    Lines priced per kWh that name a time class are divided by that class's kWh; the others
    (energy savings certificates, excise, gas lines) by all the kWh of the invoice (their quantity
    can be a weighted one). Amounts rather than unit prices: an invoice can bill two periods with
    two prices (split at the 1st of January).
    """
    # Certificates are priced per "kWhc" (weighted kWh) since November 2026: shared lines too.
    kwh_lines = [
        x for x in parsed.lines if x.price_unit in ("kWh", "kWhc") and x.amount_excl_tax is not None
    ]
    supply = [x for x in kwh_lines if x.quantity and x.unit == "kWh" and _supply_class(x.label)]
    kwh_by_class: dict[str, float] = defaultdict(float)
    for x in supply:
        kwh_by_class[_supply_class(x.label) or "base"] += x.quantity or 0.0
    total_kwh = sum(kwh_by_class.values())
    by_class: dict[str, float] = {}
    if total_kwh > 0:
        shared = 0.0
        specific: dict[str, float] = defaultdict(float)
        for x in kwh_lines:
            cls = _class_of(x.label)
            if cls and kwh_by_class.get(cls):
                specific[cls] += (x.amount_excl_tax or 0.0) / kwh_by_class[cls]
            else:
                shared += (x.amount_excl_tax or 0.0) / total_kwh
        by_class = {c: round(specific.get(c, 0.0) + shared, 5) for c in kwh_by_class}
    weighted = (
        round(sum(by_class[c] * kwh_by_class[c] for c in by_class) / total_kwh, 5)
        if by_class
        else None
    )
    starts = [x.start for x in supply if x.start] or [x.start for x in parsed.lines if x.start]
    ends = [x.end for x in supply if x.end] or [x.end for x in parsed.lines if x.end]
    refs = {k: v for k, v in parsed.references.items() if k != "invoice_date" and v is not None}
    return InvoiceDetail(
        invoice_date=parsed.references.get("invoice_date"),
        period_start=min(starts) if starts else None,
        period_end=max(ends) if ends else None,
        kwh=round(total_kwh, 3) if total_kwh else None,
        marginal_price=weighted,
        marginal_price_by_class={} if set(by_class) == {"base"} else by_class,
        subscribed_kva=parsed.subscribed_kva,
        reached_kva=parsed.reached_kva,
        overrun_hours=parsed.overrun_hours,
        references=refs,
    )


def read_invoice(data: bytes) -> InvoiceDetail:
    """PDF bytes → summary. Blocking (run it in the executor)."""
    return summarize_invoice(parse_text(pdf_text(data)))


@dataclass(frozen=True, slots=True)
class PdfSummary:
    """One delivery point, from the PDFs of its invoices over the last 12 months."""

    last: InvoiceDetail | None = None  # the most recent invoice read
    invoices: int = 0  # invoices read over 12 months
    max_reached_kva: float | None = None
    max_reached_on: dt.date | None = None  # end of the period where it was reached
    reached_kva_by_class: dict[str, float] = field(default_factory=dict)  # 12-month maxima
    overrun_hours: float | None = None  # 12-month total

    def as_dict(self) -> dict[str, Any]:
        return {
            "last": self.last.as_dict() if self.last else None,
            "invoices": self.invoices,
            "max_reached_kva": self.max_reached_kva,
            "max_reached_on": self.max_reached_on.isoformat() if self.max_reached_on else None,
            "reached_kva_by_class": self.reached_kva_by_class,
            "overrun_hours": self.overrun_hours,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PdfSummary | None:
        if not isinstance(data, dict):
            return None
        on = data.get("max_reached_on")
        try:
            on_day = dt.date.fromisoformat(on) if isinstance(on, str) else None
        except ValueError:
            on_day = None
        by_class = data.get("reached_kva_by_class")
        count = data.get("invoices")

        def opt(v: Any) -> float | None:
            return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

        return cls(
            last=InvoiceDetail.from_dict(data.get("last")),
            invoices=count if isinstance(count, int) else 0,
            max_reached_kva=opt(data.get("max_reached_kva")),
            max_reached_on=on_day,
            reached_kva_by_class={
                str(k): float(v) for k, v in by_class.items() if isinstance(v, int | float)
            }
            if isinstance(by_class, dict)
            else {},
            overrun_hours=opt(data.get("overrun_hours")),
        )


def summarize_details(details: list[InvoiceDetail]) -> PdfSummary | None:
    """Details of one point's invoices (any order) → its summary."""
    if not details:
        return None
    ordered = sorted(
        details, key=lambda d: (d.invoice_date or dt.date.min, d.period_end or dt.date.min)
    )
    by_class: dict[str, float] = {}
    best: tuple[float, dt.date | None] | None = None
    overrun = 0.0
    seen_overrun = False
    for d in ordered:
        for cls, kva in d.reached_kva.items():
            by_class[cls] = max(by_class.get(cls, 0.0), kva)
            if best is None or kva > best[0]:
                best = (kva, d.period_end)
        if d.overrun_hours:
            seen_overrun = True
            overrun += sum(d.overrun_hours.values())
    return PdfSummary(
        last=ordered[-1],
        invoices=len(ordered),
        max_reached_kva=best[0] if best else None,
        max_reached_on=best[1] if best else None,
        reached_kva_by_class=by_class,
        overrun_hours=overrun if seen_overrun else None,
    )
