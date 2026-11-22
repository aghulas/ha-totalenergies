"""Fixtures for the Home Assistant side (pytest-homeassistant-custom-component)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import date
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from .common import NOW, TODAY, FakeClient


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Any,  # first: the recorder must be set up before `hass`
    enable_custom_integrations: None,
) -> Iterator[None]:
    yield


@pytest.fixture
async def te(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> AsyncIterator[FakeClient]:
    """Paris time, a frozen clock, and a fake client serving two years of invoices."""
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to(NOW)
    client = FakeClient()
    client.fill(date(2024, 1, 1), TODAY)
    with (
        patch("custom_components.totalenergies_pro.TotalEnergiesClient", side_effect=client.build),
        patch(
            "custom_components.totalenergies_pro.coordinator.read_invoice", side_effect=client.read
        ),
    ):
        yield client
