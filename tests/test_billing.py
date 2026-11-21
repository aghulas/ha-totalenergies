"""Pure billing logic: series spread over real dates, cancelled invoices, prices, summaries."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.totalenergies_pro.api import (
    ConsumptionLine,
    Contract,
    Invoice,
    Site,
)
from custom_components.totalenergies_pro.billing import (
    SPREAD_PERIOD_END,
    ContractInfo,
    InvoiceInfo,
    PointSummary,
    build_series,
    invoice_points,
    summarize,
)

PRM = "00000000000001"
PCE = "GI000001"
POINTS = {PRM: "electricity", PCE: "gas"}


def inv(
    number: str,
    day: date,
    energy: str = "electricity",
    excl: float = 200.0,
    incl: float = 240.0,
    kwh: float | None = 1300.0,
    cancelled: bool = False,
) -> Invoice:
    return Invoice(
        number=number,
        date=day,
        energy=energy,  # type: ignore[arg-type]
        type="FACTURE",
        account_contract=None,
        contract=None,
        brand_id=None,
        points=1,
        energy_kwh=kwh,
        subtotal_excl_tax=150.0,
        taxes_excl_vat=50.0,
        total_excl_vat=excl,
        vat=incl - excl,
        total_incl_vat=incl,
        printed_on=day,
        cancellation_invoice="100000000999" if cancelled else None,
        cancellation_printed_on=day if cancelled else None,
        delivery=None,
        chorus_status=None,
    )


def ln(
    number: str,
    start: date,
    end: date,
    cls: str | None,
    kwh: float,
    ht: float,
    point: str = PRM,
) -> ConsumptionLine:
    return ConsumptionLine(
        point=point,
        energy="electricity" if point == PRM else "gas",
        start=start,
        end=end,
        time_class=cls,
        rank=None,
        supplier_calendar=None,
        energy_kwh=kwh,
        amount_excl_tax=ht,
        invoice=number,
        invoice_date=None,
    )


def site(point: str, active: bool, start: date, powers: dict[str, float]) -> Site:
    return Site(
        point=point,
        energy="electricity",
        segment="C4",
        designation="SITE SYNTH",
        communicating=True,
        account=None,
        brand_id=None,
        contract=Contract(
            number="400000000001",
            status="active" if active else "terminated",
            status_code=None,
            start=start,
            termination=None if active else date(2024, 12, 31),
            active=active,
            tariff_formula="BTSUPCU4",
            tariff_option=None,
            subscribed_power_kva=powers,
            green_energy_pct=None,
        ),
        meter="M1",
        reading_frequency="M",
    )


JAN1, JAN10 = date(2026, 1, 1), date(2026, 1, 10)


def test_lines_are_spread_over_their_period() -> None:
    lines = [ln("1", JAN1, JAN10, "HPH", 100.0, 12.0), ln("1", JAN1, JAN10, "HCH", 50.0, 4.5)]
    series = build_series([inv("1", date(2026, 1, 20))], lines, POINTS)
    s = series[PRM]
    assert set(s.billed_energy) == {JAN1 + timedelta(days=n) for n in range(10)}
    assert s.billed_energy[JAN1] == pytest.approx(15.0)
    assert sum(s.billed_energy.values()) == pytest.approx(150.0)
    assert sum(s.billed_energy_by_class["hph"].values()) == pytest.approx(100.0)
    assert sum(s.energy_cost.values()) == pytest.approx(16.5)
    # The invoice total goes on the same days, not on the invoice date.
    assert set(s.cost_incl_vat) == set(s.billed_energy)
    assert sum(s.cost_incl_vat.values()) == pytest.approx(240.0)
    assert sum(s.cost_excl_vat.values()) == pytest.approx(200.0)
    assert series[PCE].billed_energy == {}


def test_period_end_method() -> None:
    lines = [ln("1", JAN1, JAN10, "HPH", 100.0, 12.0)]
    s = build_series([inv("1", date(2026, 1, 20))], lines, POINTS, SPREAD_PERIOD_END)[PRM]
    assert s.billed_energy == {JAN10: 100.0}
    assert s.cost_incl_vat == {JAN10: 240.0}


def test_invoice_with_two_periods_is_spread_over_both() -> None:
    lines = [
        ln("1", date(2025, 12, 25), date(2025, 12, 31), "HPH", 70.0, 7.0),
        ln("1", JAN1, date(2026, 1, 3), "HPH", 30.0, 3.0),
    ]
    s = build_series([inv("1", date(2026, 2, 25), incl=110.0)], lines, POINTS)[PRM]
    assert len(s.cost_incl_vat) == 10
    assert s.cost_incl_vat[JAN1] == pytest.approx(11.0)


def test_invoice_without_lines_lands_on_its_date_for_the_only_point_of_its_energy() -> None:
    credit = inv("2", date(2026, 2, 5), energy="gas", excl=-30.0, incl=-36.0, kwh=None)
    s = build_series([credit], [], POINTS)
    assert s[PCE].cost_incl_vat == {date(2026, 2, 5): -36.0}
    assert s[PRM].cost_incl_vat == {}
    # Two gas points: ambiguous, ignored.
    two = {**POINTS, "GI000002": "gas"}
    assert invoice_points([credit], [], two) == {}


def test_cancelled_invoices_and_their_lines_count_for_nothing() -> None:
    lines = [ln("1", JAN1, JAN10, "HPH", 100.0, 12.0), ln("3", JAN1, JAN10, "HPH", 100.0, 12.0)]
    invoices = [inv("1", date(2026, 1, 20), cancelled=True), inv("3", date(2026, 2, 1))]
    s = build_series(invoices, lines, POINTS)[PRM]
    assert sum(s.billed_energy.values()) == pytest.approx(100.0)
    assert sum(s.cost_incl_vat.values()) == pytest.approx(240.0)


def test_lines_of_other_points_or_without_period_are_ignored() -> None:
    lines = [
        ln("1", JAN1, JAN10, "HPH", 100.0, 12.0, point="99999999999999"),
        ConsumptionLine(PRM, "electricity", None, JAN10, "HPH", None, None, 5, 1, "1", None),
        ConsumptionLine(PRM, "electricity", JAN10, JAN1, "HPH", None, None, 5, 1, "1", None),
    ]
    s = build_series([], lines, POINTS)[PRM]
    assert s.billed_energy == {}


def test_since_drops_the_oldest_days() -> None:
    lines = [ln("1", JAN1, JAN10, "HPH", 100.0, 12.0)]
    s = build_series([inv("1", date(2026, 1, 20))], lines, POINTS, since=date(2026, 1, 6))[PRM]
    assert min(s.billed_energy) == date(2026, 1, 6)
    assert min(s.billed_energy_by_class["hph"]) == date(2026, 1, 6)
    assert min(s.cost_incl_vat) == date(2026, 1, 6)


def test_summary_prices_and_contract() -> None:
    today = date(2026, 3, 15)
    invoices = [
        inv("1", date(2025, 2, 10), excl=999.0, incl=999.0),  # older than 12 months
        inv("2", date(2026, 1, 10), excl=200.0, incl=240.0, kwh=1000.0),
        inv("3", date(2026, 2, 10), excl=300.0, incl=360.0, kwh=None),  # kWh from its lines
    ]
    lines = [
        ln("2", date(2025, 11, 24), date(2025, 12, 23), "HPH", 1000.0, 120.0),
        ln("3", date(2025, 12, 24), date(2026, 1, 23), "HPH", 1500.0, 195.0),
        ln("3", date(2025, 12, 24), date(2026, 1, 23), "HCH", 500.0, 45.0),
    ]
    sites = [
        site(PRM, True, date(2025, 1, 1), {"hph": 42.0, "peak": 36.0}),
        site(PRM, False, date(2023, 1, 1), {}),
    ]
    out = summarize(invoices, lines, sites, POINTS, today)
    s = out[PRM]
    assert s.last_invoice is not None
    assert s.last_invoice.number == "3"
    assert (s.last_invoice.period_start, s.last_invoice.period_end) == (
        date(2025, 12, 24),
        date(2026, 1, 23),
    )
    assert s.last_invoice.energy_kwh == 2000.0
    assert s.energy_price == pytest.approx(0.12)
    assert s.energy_price_by_class == {"hph": 0.13, "hch": 0.09}
    assert s.invoices_12_months == 2
    assert s.billed_12_months_kwh == 3000.0
    assert s.average_price_excl_vat == pytest.approx(500 / 3000, abs=1e-5)
    assert s.average_price_incl_vat == pytest.approx(0.2)
    assert s.contract is not None
    assert (s.contract.status, s.contract.max_power_kva) == ("active", 42.0)
    assert out[PCE] == PointSummary()


def test_serialization_roundtrip() -> None:
    summary = PointSummary(
        last_invoice=InvoiceInfo("1", JAN1, JAN1, JAN10, 10.0, 1.0, 2.0, 3.0, 0.6, 3.6),
        energy_price=0.1,
        energy_price_by_class={"hph": 0.12},
        average_price_excl_vat=0.2,
        average_price_incl_vat=0.24,
        invoiced_12_months_excl_vat=100.0,
        invoiced_12_months_incl_vat=120.0,
        billed_12_months_kwh=500.0,
        invoices_12_months=12,
        contract=ContractInfo.from_site(site(PRM, True, JAN1, {"hph": 42.0})),
    )
    assert PointSummary.from_dict(summary.as_dict()) == summary
    assert PointSummary.from_dict(None) == PointSummary()
    assert PointSummary.from_dict({"last_invoice": {"number": 3}, "contract": "x"}) == (
        PointSummary()
    )
    odd = ContractInfo.from_dict({"subscribed_power_kva": {"hph": "x"}, "communicating": 1})
    assert odd is not None
    assert (odd.status, odd.subscribed_power_kva, odd.communicating) == ("unknown", {}, None)
    assert odd.max_power_kva is None
