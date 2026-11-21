"""Manual refresh. Guarded by the coordinator: no call after a refusal or during an anti-bot
block, and not more often than every few minutes."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    async_add_entities(TotalEnergiesRefresh(coordinator, p, "refresh") for p in coordinator.points)


class TotalEnergiesRefresh(TotalEnergiesEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self.coordinator.async_manual_refresh()
