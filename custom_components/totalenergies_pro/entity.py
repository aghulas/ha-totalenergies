"""Base entity: one device per delivery point (PRM / PCE)."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .billing import PointSummary
from .const import DOMAIN
from .coordinator import TotalEnergiesCoordinator


class TotalEnergiesEntity(CoordinatorEntity[TotalEnergiesCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TotalEnergiesCoordinator, point: str, key: str) -> None:
        super().__init__(coordinator)
        self._point = point
        self._attr_translation_key = key
        self._attr_unique_id = f"{point}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, point)},
            name=f"TotalEnergies {coordinator.point_label(point)}",
            manufacturer="TotalEnergies",
            model=str((coordinator.points.get(point) or {}).get("segment") or "") or None,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def summary(self) -> PointSummary | None:
        data = self.coordinator.data
        return data.points.get(self._point) if data else None

    @property
    def available(self) -> bool:
        # Billing values stay valid when a fetch fails.
        return self.summary is not None
