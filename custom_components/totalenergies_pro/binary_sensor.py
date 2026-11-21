"""Connection problem: the last fetch failed, TotalEnergies refused the credentials, or the
anti-bot protection is blocking the integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TotalEnergiesConfigEntry
from .entity import TotalEnergiesEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TotalEnergiesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(TotalEnergiesProblem(coordinator, p, "problem") for p in coordinator.points)


class TotalEnergiesProblem(TotalEnergiesEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        state = self.coordinator.state
        return state.auth_refused or state.last_result not in (None, "ok", "running")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.state
        return {
            "reauthentication_needed": state.auth_refused,
            "blocked_until": state.blocked_until,
            "last_attempt": state.last_attempt,
            "last_success": state.last_success,
            "last_error": state.last_error,
            "last_error_at": state.last_error_at,
        }
