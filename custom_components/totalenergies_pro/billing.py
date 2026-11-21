"""From invoices and billed consumption lines to daily series, prices and summaries.

Pure functions (no Home Assistant): the coordinator imports the series into statistics and shows
the summaries. Documented choices (docs/requirements.md, CLAUDE.md rule 4):

- **Real dates.** A line's kWh and amount belong to its consumption period (`dateDebutPeriode` →
  `dateFinPeriode`, both inclusive), not to the invoice date. They are spread evenly over the
  days of the period (default), or put on its last day (`period_end`).
- **Invoice amounts** (excl. VAT, incl. VAT: they include subscription, network and taxes) are
  spread the same way over the days of the periods of the invoice's lines, for the point those
  lines belong to. An invoice without any line (regularisation, credit note) is put on its date,
  for the only selected point of its energy — or ignored when that point is ambiguous.
- **Cancelled invoices** (`numeroFactureAnnulation` or `dateImpressionAnnulation` set) count for
  nothing, nor do their lines. Credit notes with negative amounts offset naturally.
- **Prices.** The all-in average price (excl. VAT, incl. VAT) is computed over the invoices of the
  last 12 months, not over the last invoice: a summer gas invoice is mostly fixed charges, and
  its €/kWh would be meaningless. The energy price is that of the last invoice's lines (energy
  part only, excl. tax), per time class too.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .api import ConsumptionLine, Invoice, Site

SPREAD_PER_DAY = "per_day"
SPREAD_PERIOD_END = "period_end"

AVERAGE_DAYS = 365


# ---- daily series ----------------------------------------------------------------------------


@dataclass(slots=True)
class PointSeries:
    """Daily amounts of one delivery point (keys: days, Europe/Paris)."""

    billed_energy: dict[date, float] = field(default_factory=dict)  # kWh
    billed_energy_by_class: dict[str, dict[date, float]] = field(default_factory=dict)  # kWh
    energy_cost: dict[date, float] = field(default_factory=dict)  # EUR excl. tax (lines)
    cost_excl_vat: dict[date, float] = field(default_factory=dict)  # EUR (invoices)
    cost_incl_vat: dict[date, float] = field(default_factory=dict)  # EUR (invoices)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def _add(target: dict[date, float], days: list[date], amount: float, method: str) -> None:
    if not days:
        return
    if method == SPREAD_PERIOD_END:
        last = max(days)
        target[last] = target.get(last, 0.0) + amount
        return
    share = amount / len(days)
    for day in days:
        target[day] = target.get(day, 0.0) + share


def valid_lines(
    lines: Iterable[ConsumptionLine], points: Iterable[str], invoices: Iterable[Invoice]
) -> list[ConsumptionLine]:
    """Lines of the selected points, with a period, not belonging to a cancelled invoice."""
    selected = set(points)
    cancelled = {i.number for i in invoices if i.cancelled and i.number}
    return [
        x
        for x in lines
        if x.point in selected
        and x.start is not None
        and x.end is not None
        and x.start <= x.end
        and x.invoice not in cancelled
    ]


def invoice_points(
    invoices: Iterable[Invoice], lines: Iterable[ConsumptionLine], point_energy: dict[str, str]
) -> dict[str, str]:
    """Invoice number → delivery point: the point of its lines, else the only selected point of
    its energy."""
    out: dict[str, str] = {}
    for x in lines:
        if x.invoice and x.point in point_energy:
            out.setdefault(x.invoice, x.point)
    by_energy: dict[str, list[str]] = defaultdict(list)
    for point, energy in point_energy.items():
        by_energy[energy].append(point)
    for inv in invoices:
        if inv.number and inv.number not in out and inv.energy:
            candidates = by_energy.get(inv.energy, [])
            if len(candidates) == 1:
                out[inv.number] = candidates[0]
    return out


def build_series(
    invoices: list[Invoice],
    lines: list[ConsumptionLine],
    point_energy: dict[str, str],
    method: str = SPREAD_PER_DAY,
    since: date | None = None,
) -> dict[str, PointSeries]:
    """Daily series per point. Days before `since` are dropped (the edge of a partial search,
    where lines outside the search could also contribute)."""
    series = {point: PointSeries() for point in point_energy}
    good = valid_lines(lines, point_energy, invoices)
    periods: dict[tuple[str, str], set[date]] = defaultdict(set)  # (invoice, point) → days

    for x in good:
        assert x.point is not None and x.start is not None and x.end is not None
        s = series[x.point]
        days = _days(x.start, x.end)
        if x.energy_kwh is not None:
            _add(s.billed_energy, days, x.energy_kwh, method)
            if x.time_class:
                by_class = s.billed_energy_by_class.setdefault(x.time_class.lower(), {})
                _add(by_class, days, x.energy_kwh, method)
        if x.amount_excl_tax is not None:
            _add(s.energy_cost, days, x.amount_excl_tax, method)
        if x.invoice:
            periods[(x.invoice, x.point)].update(days)

    owner = invoice_points(invoices, good, point_energy)
    for inv in invoices:
        if inv.cancelled or not inv.number or inv.number not in owner:
            continue
        point = owner[inv.number]
        days = sorted(periods.get((inv.number, point), set()))
        if not days and inv.date is not None:
            days = [inv.date]
        s = series[point]
        if inv.total_excl_vat is not None:
            _add(s.cost_excl_vat, days, inv.total_excl_vat, method)
        if inv.total_incl_vat is not None:
            _add(s.cost_incl_vat, days, inv.total_incl_vat, method)

    if since is not None:
        for s in series.values():
            for values in (
                s.billed_energy,
                s.energy_cost,
                s.cost_excl_vat,
                s.cost_incl_vat,
                *s.billed_energy_by_class.values(),
            ):
                for day in [d for d in values if d < since]:
                    del values[day]
    return series


# ---- summaries shown by the entities ---------------------------------------------------------


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


@dataclass(frozen=True, slots=True)
class InvoiceInfo:
    number: str
    date: date | None
    period_start: dt.date | None
    period_end: dt.date | None
    energy_kwh: float | None
    subtotal_excl_tax: float | None
    taxes_excl_vat: float | None
    total_excl_vat: float | None
    vat: float | None
    total_incl_vat: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "date": _iso(self.date),
            "period_start": _iso(self.period_start),
            "period_end": _iso(self.period_end),
            "energy_kwh": self.energy_kwh,
            "subtotal_excl_tax": self.subtotal_excl_tax,
            "taxes_excl_vat": self.taxes_excl_vat,
            "total_excl_vat": self.total_excl_vat,
            "vat": self.vat,
            "total_incl_vat": self.total_incl_vat,
        }

    @classmethod
    def from_dict(cls, data: Any) -> InvoiceInfo | None:
        if not isinstance(data, dict) or not isinstance(data.get("number"), str):
            return None
        return cls(
            number=data["number"],
            date=_day(data.get("date")),
            period_start=_day(data.get("period_start")),
            period_end=_day(data.get("period_end")),
            energy_kwh=_float(data.get("energy_kwh")),
            subtotal_excl_tax=_float(data.get("subtotal_excl_tax")),
            taxes_excl_vat=_float(data.get("taxes_excl_vat")),
            total_excl_vat=_float(data.get("total_excl_vat")),
            vat=_float(data.get("vat")),
            total_incl_vat=_float(data.get("total_incl_vat")),
        )


@dataclass(frozen=True, slots=True)
class ContractInfo:
    """What the entities show of a contract (no contract or meter number)."""

    energy: str | None
    segment: str | None
    status: str
    start: date | None
    termination: date | None
    tariff_formula: str | None
    tariff_option: str | None
    subscribed_power_kva: dict[str, float]
    reading_frequency: str | None
    communicating: bool | None

    @property
    def max_power_kva(self) -> float | None:
        return max(self.subscribed_power_kva.values()) if self.subscribed_power_kva else None

    @classmethod
    def from_site(cls, site: Site) -> ContractInfo:
        c = site.contract
        return cls(
            energy=site.energy,
            segment=site.segment,
            status=c.status,
            start=c.start,
            termination=c.termination,
            tariff_formula=c.tariff_formula,
            tariff_option=c.tariff_option,
            subscribed_power_kva=dict(c.subscribed_power_kva),
            reading_frequency=site.reading_frequency,
            communicating=site.communicating,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "energy": self.energy,
            "segment": self.segment,
            "status": self.status,
            "start": _iso(self.start),
            "termination": _iso(self.termination),
            "tariff_formula": self.tariff_formula,
            "tariff_option": self.tariff_option,
            "subscribed_power_kva": self.subscribed_power_kva,
            "reading_frequency": self.reading_frequency,
            "communicating": self.communicating,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ContractInfo | None:
        if not isinstance(data, dict):
            return None
        powers = data.get("subscribed_power_kva")
        communicating = data.get("communicating")
        return cls(
            energy=data.get("energy"),
            segment=data.get("segment"),
            status=str(data.get("status") or "unknown"),
            start=_day(data.get("start")),
            termination=_day(data.get("termination")),
            tariff_formula=data.get("tariff_formula"),
            tariff_option=data.get("tariff_option"),
            subscribed_power_kva={
                str(k): float(v) for k, v in (powers or {}).items() if _float(v) is not None
            }
            if isinstance(powers, dict)
            else {},
            reading_frequency=data.get("reading_frequency"),
            communicating=communicating if isinstance(communicating, bool) else None,
        )


@dataclass(frozen=True, slots=True)
class PointSummary:
    """Last invoice, prices and contract of one delivery point."""

    last_invoice: InvoiceInfo | None = None
    energy_price: float | None = None  # EUR/kWh excl. tax, energy part of the last invoice
    energy_price_by_class: dict[str, float] = field(default_factory=dict)
    average_price_excl_vat: float | None = None  # EUR/kWh, all-in, last 12 months
    average_price_incl_vat: float | None = None
    invoiced_12_months_excl_vat: float | None = None
    invoiced_12_months_incl_vat: float | None = None
    billed_12_months_kwh: float | None = None
    invoices_12_months: int = 0
    contract: ContractInfo | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_invoice": self.last_invoice.as_dict() if self.last_invoice else None,
            "energy_price": self.energy_price,
            "energy_price_by_class": self.energy_price_by_class,
            "average_price_excl_vat": self.average_price_excl_vat,
            "average_price_incl_vat": self.average_price_incl_vat,
            "invoiced_12_months_excl_vat": self.invoiced_12_months_excl_vat,
            "invoiced_12_months_incl_vat": self.invoiced_12_months_incl_vat,
            "billed_12_months_kwh": self.billed_12_months_kwh,
            "invoices_12_months": self.invoices_12_months,
            "contract": self.contract.as_dict() if self.contract else None,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PointSummary:
        if not isinstance(data, dict):
            return cls()
        by_class = data.get("energy_price_by_class")
        count = data.get("invoices_12_months")
        return cls(
            last_invoice=InvoiceInfo.from_dict(data.get("last_invoice")),
            energy_price=_float(data.get("energy_price")),
            energy_price_by_class={
                str(k): float(v) for k, v in by_class.items() if _float(v) is not None
            }
            if isinstance(by_class, dict)
            else {},
            average_price_excl_vat=_float(data.get("average_price_excl_vat")),
            average_price_incl_vat=_float(data.get("average_price_incl_vat")),
            invoiced_12_months_excl_vat=_float(data.get("invoiced_12_months_excl_vat")),
            invoiced_12_months_incl_vat=_float(data.get("invoiced_12_months_incl_vat")),
            billed_12_months_kwh=_float(data.get("billed_12_months_kwh")),
            invoices_12_months=count if isinstance(count, int) else 0,
            contract=ContractInfo.from_dict(data.get("contract")),
        )


def _ratio(amount: float, kwh: float) -> float | None:
    return round(amount / kwh, 5) if kwh > 0 else None


def summarize(
    invoices: list[Invoice],
    lines: list[ConsumptionLine],
    sites: list[Site],
    point_energy: dict[str, str],
    today: date,
) -> dict[str, PointSummary]:
    """Per point: last invoice, energy price, 12-month averages, contract."""
    good = valid_lines(lines, point_energy, invoices)
    owner = invoice_points(invoices, good, point_energy)
    lines_of: dict[str, list[ConsumptionLine]] = defaultdict(list)
    for x in good:
        if x.invoice:
            lines_of[x.invoice].append(x)
    # The active contract of each point, else its latest one.
    contracts: dict[str, ContractInfo] = {}
    for site in sorted(sites, key=lambda s: (s.contract.active, s.contract.start or date.min)):
        if site.point in point_energy:
            contracts[site.point] = ContractInfo.from_site(site)

    out: dict[str, PointSummary] = {}
    since = today - timedelta(days=AVERAGE_DAYS)
    for point in point_energy:
        mine = [
            i
            for i in invoices
            if not i.cancelled and i.number and owner.get(i.number) == point and i.date
        ]
        mine.sort(key=lambda i: (i.date or date.min, i.number or ""))
        last_info, energy_price, by_class = None, None, {}
        if mine:
            last = mine[-1]
            assert last.number is not None
            its = lines_of.get(last.number, [])
            starts = [x.start for x in its if x.start]
            ends = [x.end for x in its if x.end]
            kwh_lines = sum(x.energy_kwh or 0.0 for x in its)
            last_info = InvoiceInfo(
                number=last.number,
                date=last.date,
                period_start=min(starts) if starts else None,
                period_end=max(ends) if ends else None,
                energy_kwh=last.energy_kwh if last.energy_kwh is not None else kwh_lines or None,
                subtotal_excl_tax=last.subtotal_excl_tax,
                taxes_excl_vat=last.taxes_excl_vat,
                total_excl_vat=last.total_excl_vat,
                vat=last.vat,
                total_incl_vat=last.total_incl_vat,
            )
            priced = [x for x in its if x.energy_kwh and x.amount_excl_tax is not None]
            energy_price = _ratio(
                sum(x.amount_excl_tax or 0.0 for x in priced),
                sum(x.energy_kwh or 0.0 for x in priced),
            )
            per_class: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
            for x in priced:
                if x.time_class:
                    acc = per_class[x.time_class.lower()]
                    acc[0] += x.amount_excl_tax or 0.0
                    acc[1] += x.energy_kwh or 0.0
            by_class = {
                k: price for k, (amount, kwh) in per_class.items() if (price := _ratio(amount, kwh))
            }

        recent = [i for i in mine if i.date and i.date > since]
        kwh = 0.0
        for inv in recent:
            assert inv.number is not None
            from_lines = sum(x.energy_kwh or 0.0 for x in lines_of.get(inv.number, []))
            kwh += inv.energy_kwh if inv.energy_kwh is not None else from_lines
        excl = sum(i.total_excl_vat or 0.0 for i in recent)
        incl = sum(i.total_incl_vat or 0.0 for i in recent)
        out[point] = PointSummary(
            last_invoice=last_info,
            energy_price=energy_price,
            energy_price_by_class=by_class,
            average_price_excl_vat=_ratio(excl, kwh) if recent else None,
            average_price_incl_vat=_ratio(incl, kwh) if recent else None,
            invoiced_12_months_excl_vat=round(excl, 2) if recent else None,
            invoiced_12_months_incl_vat=round(incl, 2) if recent else None,
            billed_12_months_kwh=round(kwh, 3) if recent else None,
            invoices_12_months=len(recent),
            contract=contracts.get(point),
        )
    return out
