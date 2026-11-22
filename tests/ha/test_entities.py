"""Entities, diagnostics (redacted) and the PDF download service."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.totalenergies_pro.api import TotalEnergiesAuthError
from custom_components.totalenergies_pro.const import CONF_PDF_FOLDER, DOMAIN

from .common import PASSWORD, PCE, PDF, PRM, USERNAME, FakeClient, make_entry, setup


def _entity(hass: HomeAssistant, point: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor"
        if key not in ("problem", "refresh")
        else ("binary_sensor" if key == "problem" else "button"),
        DOMAIN,
        f"{point}_{key}",
    )
    assert entity_id is not None, key
    return entity_id


async def test_sensors(hass: HomeAssistant, te: FakeClient) -> None:
    entry = make_entry()
    await setup(hass, entry)

    last = hass.states.get(_entity(hass, PRM, "last_invoice"))
    assert last is not None
    assert float(last.state) == 240.0
    assert last.attributes["unit_of_measurement"] == "€"
    assert last.attributes["period_start"] == date(2026, 1, 24)
    assert last.attributes["energy_kwh"] == 1300.0

    price = hass.states.get(_entity(hass, PRM, "average_price_incl_vat"))
    assert price is not None
    assert price.attributes["unit_of_measurement"] == "€/kWh"
    assert float(price.state) == pytest.approx(240 / 1300, abs=1e-4)
    energy = hass.states.get(_entity(hass, PRM, "energy_price"))
    assert energy is not None
    assert float(energy.state) == pytest.approx(147 / 1300, abs=1e-4)
    assert energy.attributes["price_hph"] == 0.12
    gas_price = hass.states.get(_entity(hass, PCE, "average_price_excl_vat"))
    assert gas_price is not None
    assert float(gas_price.state) == pytest.approx(0.08)

    assert hass.states.get(_entity(hass, PRM, "last_invoice_date")).state == "2026-03-10"  # type: ignore[union-attr]
    assert hass.states.get(_entity(hass, PRM, "contract_status")).state == "active"  # type: ignore[union-attr]
    assert hass.states.get(_entity(hass, PRM, "subscribed_power")).state == "42.0"  # type: ignore[union-attr]
    twelve = hass.states.get(_entity(hass, PCE, "invoiced_12_months"))
    assert twelve is not None
    assert float(twelve.state) == pytest.approx(12 * 480.0)
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{PCE}_subscribed_power") is None
    )
    assert hass.states.get(_entity(hass, PRM, "problem")).state == "off"  # type: ignore[union-attr]

    # From the invoice PDFs (the newest six invoices are read at setup).
    marginal = hass.states.get(_entity(hass, PRM, "marginal_price"))
    assert marginal is not None
    assert float(marginal.state) == 0.2
    assert marginal.attributes["price_hph"] == 0.22
    assert marginal.attributes["price_incl_vat"] == 0.24
    power = hass.states.get(_entity(hass, PRM, "max_reached_power"))
    assert power is not None
    assert power.attributes["unit_of_measurement"] == "kVA"
    assert power.attributes["subscribed_hph"] == 42.0
    assert "reached_hph" in power.attributes
    car = hass.states.get(_entity(hass, PCE, "reference_consumption"))
    assert car is not None
    assert float(car.state) == 150000.0
    assert car.attributes["network_tariff"] == "T3"
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{PCE}_max_reached_power") is None
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{PRM}_reference_consumption") is None


async def test_contract_sensors_fall_back_on_the_pdf(hass: HomeAssistant, te: FakeClient) -> None:
    te.sites = []  # TotalEnergies withholds the list of sites
    entry = make_entry()
    await setup(hass, entry)
    assert hass.states.get(_entity(hass, PCE, "tariff")).state == "T3"  # type: ignore[union-attr]
    elec = hass.states.get(_entity(hass, PRM, "tariff"))
    assert elec is not None
    assert elec.state == "C4"
    assert elec.attributes["option"] == "Courte Utilisation"
    assert elec.attributes["connection"] == "BT > 36 kVA"
    power = hass.states.get(_entity(hass, PRM, "subscribed_power"))
    assert power is not None
    assert power.state == "42.0"
    assert power.attributes["power_hph"] == 42.0


async def test_problem_sensor_and_button(hass: HomeAssistant, te: FakeClient) -> None:
    te.error = TotalEnergiesAuthError("refused", 200)
    entry = make_entry()
    await setup(hass, entry)
    problem = hass.states.get(_entity(hass, PRM, "problem"))
    assert problem is not None
    assert problem.state == "on"
    assert problem.attributes["reauthentication_needed"] is True
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": _entity(hass, PRM, "refresh")}, blocking=True
        )


async def test_diagnostics_are_redacted(
    hass: HomeAssistant, te: FakeClient, hass_client: ClientSessionGenerator
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = json.dumps(diag)
    for secret in (USERNAME, PASSWORD, PRM, PCE.lower(), PCE, "10000000", "20000000", "SITE"):
        assert secret not in text, secret
    assert diag["entry"]["points"] == 2
    assert diag["points"]["point_1"]["energy"] == "electricity"
    assert diag["points"]["point_1"]["last_invoice"]["total_incl_vat"] == 240.0
    assert diag["state"]["last_result"] == "ok"


async def test_download_service(hass: HomeAssistant, te: FakeClient, tmp_path: Path) -> None:
    entry = make_entry()
    await setup(hass, entry)
    with pytest.raises(ServiceValidationError, match="folder"):
        await hass.services.async_call(DOMAIN, "download_invoice", {}, blocking=True)

    folder = tmp_path / "factures"
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PDF_FOLDER: str(folder)}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await hass.services.async_call(
        DOMAIN, "download_invoice", {}, blocking=True, return_response=True
    )
    files = result["files"]  # type: ignore[index]
    assert len(files) == 2  # the last invoice of each point
    assert all(f["written"] for f in files)  # type: ignore[index]
    pdfs = sorted(folder.glob("TotalEnergies_*.pdf"))
    assert [p.read_bytes() for p in pdfs] == [PDF, PDF]
    assert oct(pdfs[0].stat().st_mode & 0o777) == "0o600"

    # Existing files are never overwritten nor downloaded again.
    te.calls.clear()
    te.pdf = b"%PDF other"
    result = await hass.services.async_call(
        DOMAIN,
        "download_invoice",
        {"invoice": pdfs[0].stem.split("_")[1]},
        blocking=True,
        return_response=True,
    )
    assert result["files"][0]["written"] is False  # type: ignore[index]
    assert te.calls == []
    assert pdfs[0].read_bytes() == PDF

    with pytest.raises(ServiceValidationError, match="digits"):
        await hass.services.async_call(
            DOMAIN, "download_invoice", {"invoice": "12/../x"}, blocking=True
        )

    hass.config.allowlist_external_dirs = set()
    with pytest.raises(ServiceValidationError, match="not allowed"):
        await hass.services.async_call(DOMAIN, "download_invoice", {}, blocking=True)
