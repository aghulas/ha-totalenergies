"""Sensors: last invoice, prices (for the Energy dashboard), 12-month totals, contract.

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


@dataclass(frozen=True, kw_only=True)
class TotalEnergiesSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PointSummary], Value]
    attrs_fn: Callable[[PointSummary], dict[str, Any]] | None = None
    electricity_only: bool = False


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
        value_fn=lambda s: s.contract.tariff_formula if s.contract else None,
        attrs_fn=lambda s: {"option": s.contract.tariff_option} if s.contract else {},
    ),
    TotalEnergiesSensorDescription(
        key="subscribed_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.KILO_VOLT_AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        electricity_only=True,
        value_fn=lambda s: s.contract.max_power_kva if s.contract else None,
        attrs_fn=lambda s: (
            {f"power_{k}": v for k, v in sorted(s.contract.subscribed_power_kva.items())}
            if s.contract
            else {}
        ),
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
        if not description.electricity_only or info.get("energy") == "electricity"
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
