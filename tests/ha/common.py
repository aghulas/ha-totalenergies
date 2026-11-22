"""Test helpers for the Home Assistant side: a fake TotalEnergies client and synthetic data."""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.totalenergies_pro.api import (
    ConsumptionLine,
    ConsumptionSearch,
    Contract,
    Invoice,
    InvoiceSearch,
    Site,
    Tokens,
)
from custom_components.totalenergies_pro.const import CONF_HISTORY_YEARS, CONF_POINTS, DOMAIN

PRM = "00000000000001"
PCE = "GI000001"
USERNAME = "compte@exemple.invalid"
PASSWORD = "motdepasse-synth"
NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)  # 13:00 in Paris, after the fetch hour
TODAY = date(2026, 3, 15)
ENTRY_DATA: dict[str, Any] = {
    "username": USERNAME,
    "password": PASSWORD,
    CONF_POINTS: {
        PRM: {"energy": "electricity", "segment": "C4", "label": "Electricity C4"},
        PCE: {"energy": "gas", "segment": "T3", "label": "Gas T3"},
    },
}
PDF = b"%PDF-1.4 synthetic\n%%EOF"


def make_entry(unique_id: str | None = USERNAME, **options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        title="Compte de test",
        data=ENTRY_DATA,
        options={CONF_HISTORY_YEARS: 1, **options},
    )


def invoice(number: str, day: date, energy: str, kwh: float, excl: float, incl: float) -> Invoice:
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
        subtotal_excl_tax=excl * 0.8,
        taxes_excl_vat=excl * 0.2,
        total_excl_vat=excl,
        vat=round(incl - excl, 2),
        total_incl_vat=incl,
        printed_on=day,
        cancellation_invoice=None,
        cancellation_printed_on=None,
        delivery=None,
        chorus_status=None,
    )


def line(
    number: str, point: str, start: date, end: date, cls: str | None, kwh: float, ht: float
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


def site(point: str, energy: str, segment: str, powers: dict[str, float]) -> Site:
    return Site(
        point=point,
        energy=energy,  # type: ignore[arg-type]
        segment=segment,
        designation="SITE SYNTH",
        communicating=True,
        account="300000000001",
        brand_id=None,
        contract=Contract(
            number="400000000001",
            status="active",
            status_code="45",
            start=date(2025, 1, 1),
            termination=None,
            active=True,
            tariff_formula="BTSUPCU4" if energy == "electricity" else None,
            tariff_option=None,
            subscribed_power_kva=powers,
            green_energy_pct=None,
        ),
        meter="M1",
        reading_frequency="M",
    )


class FakeClient:
    """Stands in for TotalEnergiesClient: serves synthetic invoices, records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.invoices: list[Invoice] = []
        self.lines: list[ConsumptionLine] = []
        self.sites = [
            site(PRM, "electricity", "C4", {"hph": 42.0, "hch": 42.0, "peak": 36.0}),
            site(PCE, "gas", "T3", {}),
        ]
        self.auth_error: Exception | None = None
        self.blocked_until: float | None = None
        self.tokens: Tokens | None = None
        self.kwargs: dict[str, Any] = {}
        self.pdf = PDF
        self.sites_error: Exception | None = None

    def build(self, *args: Any, **kwargs: Any) -> FakeClient:
        self.kwargs = kwargs
        return self

    def fill(self, first: date, last: date) -> None:
        """Monthly invoices: electricity read on the 24th, gas by calendar month; each issued
        on the 10th of the month after the period."""
        n = 0
        month = date(first.year, first.month, 1)
        while month <= last:
            year, mon = month.year, month.month
            nxt = date(year + (mon == 12), mon % 12 + 1, 1)
            issued = date(nxt.year, nxt.month, 10)
            if issued > last:
                break
            n += 1
            # electricity: 24 of the month before → 23 of this month
            prev = month - timedelta(days=1)
            e_start, e_end = date(prev.year, prev.month, 24), date(year, mon, 23)
            num = f"10000000{n:04d}"
            self.lines += [
                line(num, PRM, e_start, e_end, "HPH", 1000.0, 120.0),
                line(num, PRM, e_start, e_end, "HCH", 300.0, 27.0),
            ]
            self.invoices.append(invoice(num, issued, "electricity", 1300.0, 200.0, 240.0))
            gnum = f"20000000{n:04d}"
            g_end = date(year, mon, calendar.monthrange(year, mon)[1])
            self.lines.append(line(gnum, PCE, month, g_end, None, 5000.0, 300.0))
            self.invoices.append(invoice(gnum, issued, "gas", 5000.0, 400.0, 480.0))
            month = nxt

    def _call(self, *call: Any) -> None:
        self.calls.append(call)
        if self.error is not None:
            raise self.error

    async def async_login(self) -> None:
        self._call("login")

    async def async_get_sites(self) -> list[Site]:
        self._call("sites")
        if self.sites_error is not None:
            raise self.sites_error
        return self.sites

    def _issued(self, number: str | None) -> date | None:
        return next((i.date for i in self.invoices if i.number == number), None)

    async def async_get_invoices(self, start: date, end: date) -> InvoiceSearch:
        self._call("invoices", start, end)
        found = [i for i in self.invoices if i.date and start <= i.date <= end]
        return InvoiceSearch(sorted(found, key=lambda i: i.date or date.min, reverse=True), False)

    async def async_get_consumptions(self, start: date, end: date) -> ConsumptionSearch:
        self._call("consumptions", start, end)
        found = [x for x in self.lines if start <= (self._issued(x.invoice) or date.min) <= end]
        return ConsumptionSearch(found, True)  # TotalEnergies flags its searches as partial

    async def async_download_invoice(self, number: str) -> bytes:
        self._call("pdf", number)
        return self.pdf


async def stats(hass: HomeAssistant, statistic_id: str) -> list[dict[str, Any]]:
    """All rows of an external statistic, once the recorder has processed its queue."""
    from homeassistant.components.recorder.statistics import statistics_during_period

    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(1970, 1, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return list(rows.get(statistic_id, []))


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def move(hass: HomeAssistant, freezer: FrozenDateTimeFactory, to: datetime) -> None:
    freezer.move_to(to)
    async_fire_time_changed(hass, to)
    await hass.async_block_till_done(wait_background_tasks=True)
