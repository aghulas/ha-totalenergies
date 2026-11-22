"""Sensors: last invoice, prices, 12-month totals, contract, and what the invoice PDFs add
(price of one more kWh, power reached, gas reference consumption).

History itself (billed kWh and costs per day) lives in the external statistics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CURRENCY_EURO,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .billing import PointSummary
from .coordinator import TotalEnergiesConfigEntry, TotalEnergiesCoordinator
from .entity import TotalEnergiesEntity
from .invoice_pdf import PdfSummary

type Value = str | float | date | None

PRICE_UNIT = f"{CURRENCY_EURO}/{UnitOfEnergy.KILO_WATT_HOUR}"
CONTRACT_STATUSES = ["active", "changing", "terminating", "terminated", "unknown"]


def _invoice_attrs(s: PointSummary) -> dict[str, Any]:
    i = s.last_invoice
    if i is None:
        return {}
    return {
        "invoice": i.number,
        "date": i.date,
        "period_start": i.period_start,
        "period_end": i.period_end,
        "energy_kwh": i.energy_kwh,
        "subtotal_excl_tax": i.subtotal_excl_tax,
        "taxes_excl_vat": i.taxes_excl_vat,
        "total_excl_vat": i.total_excl_vat,
        "vat": i.vat,
        "total_incl_vat": i.total_incl_vat,
    }


def _contract_attrs(s: PointSummary) -> dict[str, Any]:
    c = s.contract
    if c is None:
        return {}
    return {
        "start": c.start,
        "termination": c.termination,
        "segment": c.segment,
        "tariff_formula": c.tariff_formula,
        "tariff_option": c.tariff_option,
        "reading_frequency": c.reading_frequency,
        "communicating": c.communicating,
    }


def _pdf_ref(s: PointSummary, key: str) -> Any:
    last = s.pdf.last if s.pdf else None
    return last.references.get(key) if last else None


def _marginal_attrs(s: PointSummary) -> dict[str, Any]:
    last = s.pdf.last if s.pdf else None
    if last is None:
        return {}
    attrs: dict[str, Any] = {
        f"price_{k}": v for k, v in sorted(last.marginal_price_by_class.items())
    }
    if last.marginal_price is not None:
        attrs["price_incl_vat"] = round(last.marginal_price * 1.2, 5)
    attrs.update(period_start=last.period_start, period_end=last.period_end)
    return attrs


def _power_attrs(s: PointSummary) -> dict[str, Any]:
    p: PdfSummary | None = s.pdf
    if p is None:
        return {}
    attrs: dict[str, Any] = {f"reached_{k}": v for k, v in sorted(p.reached_kva_by_class.items())}
    subscribed = p.last.subscribed_kva if p.last else {}
    attrs.update({f"subscribed_{k}": v for k, v in sorted(subscribed.items())})
    attrs.update(reached_on=p.max_reached_on, overrun_hours=p.overrun_hours, invoices=p.invoices)
    return attrs


def _gas_attrs(s: PointSummary) -> dict[str, Any]:
    keys = ("network_tariff", "profile", "zone", "regional_level", "conversion_factor", "offer")
    attrs = {k: v for k in keys if (v := _pdf_ref(s, k)) is not None}
    attrs["billed_12_months_kwh"] = s.billed_12_months_kwh
    return attrs


def _tariff(s: PointSummary) -> str | None:
    if s.contract and s.contract.tariff_formula:
        return s.contract.tariff_formula
    value = _pdf_ref(s, "network_tariff")  # gas: T1…T4 on the invoice
    return str(value) if value else None


def _subscribed(s: PointSummary) -> float | None:
    if s.contract and s.contract.max_power_kva is not None:
        return s.contract.max_power_kva
    last = s.pdf.last if s.pdf else None  # the invoice prints it too
    powers = [v for k, v in (last.subscribed_kva if last else {}).items() if k != "weighted"]
    return max(powers) if powers else None


@dataclass(frozen=True, kw_only=True)
class TotalEnergiesSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PointSummary], Value]
    attrs_fn: Callable[[PointSummary], dict[str, Any]] | None = None
    electricity_only: bool = False
    gas_only: bool = False


SENSORS: tuple[TotalEnergiesSensorDescription, ...] = (
    TotalEnergiesSensorDescription(
        key="last_invoice",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_EURO,
        suggested_display_precision=2,
        value_fn=lambda s: s.last_invoice.total_incl_vat if s.last_invoice else None,
        attrs_fn=_invoice_attrs,
    ),
    TotalEnergiesSensorDescription(
        key="last_invoice_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda s: s.last_invoice.date if s.last_invoice else None,
    ),
    TotalEnergiesSensorDescription(
        key="last_invoice_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        value_fn=lambda s: s.last_invoice.energy_kwh if s.last_invoice else None,
    ),
    TotalEnergiesSensorDescription(
        key="average_price_excl_vat",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda s: s.average_price_excl_vat,
        attrs_fn=lambda s: {
            "invoiced_excl_vat": s.invoiced_12_months_excl_vat,
            "billed_kwh": s.billed_12_months_kwh,
            "invoices": s.invoices_12_months,
        },
    ),
    TotalEnergiesSensorDescription(
        key="average_price_incl_vat",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda s: s.average_price_incl_vat,
        attrs_fn=lambda s: {
            "invoiced_incl_vat": s.invoiced_12_months_incl_vat,
            "billed_kwh": s.billed_12_months_kwh,
            "invoices": s.invoices_12_months,
        },
    ),
    TotalEnergiesSensorDescription(
        key="energy_price",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda s: s.energy_price,
        attrs_fn=lambda s: {f"price_{k}": v for k, v in sorted(s.energy_price_by_class.items())},
    ),
    TotalEnergiesSensorDescription(
        key="invoiced_12_months",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_EURO,
        suggested_display_precision=2,
        value_fn=lambda s: s.invoiced_12_months_incl_vat,
        attrs_fn=lambda s: {
            "invoiced_excl_vat": s.invoiced_12_months_excl_vat,
            "billed_kwh": s.billed_12_months_kwh,
            "invoices": s.invoices_12_months,
        },
    ),
    TotalEnergiesSensorDescription(
        key="contract_status",
        device_class=SensorDeviceClass.ENUM,
        options=CONTRACT_STATUSES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.contract.status if s.contract else None,
        attrs_fn=_contract_attrs,
    ),
    TotalEnergiesSensorDescription(
        key="contract_start",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.contract.start if s.contract else None,
    ),
    TotalEnergiesSensorDescription(
        key="tariff",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_tariff,
        attrs_fn=lambda s: {
            "option": (s.contract.tariff_option if s.contract else None)
            or _pdf_ref(s, "tariff_option")
        },
    ),
    TotalEnergiesSensorDescription(
        key="subscribed_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.KILO_VOLT_AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        electricity_only=True,
        value_fn=_subscribed,
        attrs_fn=lambda s: (
            {f"power_{k}": v for k, v in sorted(s.contract.subscribed_power_kva.items())}
            if s.contract and s.contract.subscribed_power_kva
            else {
                f"power_{k}": v
                for k, v in sorted(
                    (s.pdf.last.subscribed_kva if s.pdf and s.pdf.last else {}).items()
                )
            }
        ),
    ),
    # Read from the invoice PDFs (option, on by default).
    TotalEnergiesSensorDescription(
        key="marginal_price",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda s: s.pdf.last.marginal_price if s.pdf and s.pdf.last else None,
        attrs_fn=_marginal_attrs,
    ),
    TotalEnergiesSensorDescription(
        key="max_reached_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.KILO_VOLT_AMPERE,
        electricity_only=True,
        value_fn=lambda s: s.pdf.max_reached_kva if s.pdf else None,
        attrs_fn=_power_attrs,
    ),
    TotalEnergiesSensorDescription(
        key="reference_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        gas_only=True,
        value_fn=lambda s: _pdf_ref(s, "car_kwh"),
        attrs_fn=_gas_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TotalEnergiesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        TotalEnergiesSensor(coordinator, point, description)
        for point, info in coordinator.points.items()
        for description in SENSORS
        if (not description.electricity_only or info.get("energy") == "electricity")
        and (not description.gas_only or info.get("energy") == "gas")
    )


class TotalEnergiesSensor(TotalEnergiesEntity, SensorEntity):
    entity_description: TotalEnergiesSensorDescription

    def __init__(
        self,
        coordinator: TotalEnergiesCoordinator,
        point: str,
        description: TotalEnergiesSensorDescription,
    ) -> None:
        super().__init__(coordinator, point, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Value:
        s = self.summary
        return self.entity_description.value_fn(s) if s else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        s, attrs_fn = self.summary, self.entity_description.attrs_fn
        return attrs_fn(s) if s and attrs_fn else None
