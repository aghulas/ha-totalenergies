"""Service `totalenergies_pro.download_invoice`: save invoice PDFs to the folder chosen in the
options (off by default). Files are named `TotalEnergies_<number>.pdf` and never overwritten."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util.json import JsonValueType

from .api import TotalEnergiesAuthError, TotalEnergiesError
from .api.const import INVOICE_NUMBER_RE
from .const import (
    ATTR_CONFIG_ENTRY,
    ATTR_INVOICE,
    CONF_PDF_FOLDER,
    DOMAIN,
    SERVICE_DOWNLOAD_INVOICE,
)

SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional(ATTR_INVOICE): cv.string,
    }
)


def _write_new(path: Path, content: bytes) -> bool:
    """Create the file (mode 600) unless it exists. Returns whether it was written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return True


def _entry(hass: HomeAssistant, entry_id: str | None) -> Any:
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED and (entry_id is None or e.entry_id == entry_id)
    ]
    if len(entries) != 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="which_entry")
    return entries[0]


async def _async_download(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    entry = _entry(hass, call.data.get(ATTR_CONFIG_ENTRY))
    folder = str(entry.options.get(CONF_PDF_FOLDER) or "").strip()
    if not folder:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="pdf_disabled")
    if not hass.config.is_allowed_path(folder):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="folder_not_allowed"
        )
    coordinator = entry.runtime_data
    if ATTR_INVOICE in call.data:
        numbers = [call.data[ATTR_INVOICE].strip()]
        if not INVOICE_NUMBER_RE.fullmatch(numbers[0]):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_invoice"
            )
    else:  # the last invoice of each point
        data = coordinator.data
        numbers = sorted(
            {s.last_invoice.number for s in data.points.values() if s.last_invoice}
            if data
            else set()
        )
    if coordinator.state.auth_refused:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="reauth_needed")

    files: list[JsonValueType] = []
    for number in numbers:
        path = Path(folder) / f"TotalEnergies_{number}.pdf"
        if await hass.async_add_executor_job(path.exists):
            files.append({"file": str(path), "written": False})
            continue
        try:
            content = await coordinator.client.async_download_invoice(number)
        except TotalEnergiesAuthError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="reauth_needed"
            ) from err
        except TotalEnergiesError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="download_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        written = await hass.async_add_executor_job(_write_new, path, content)
        files.append({"file": str(path), "written": written})
    return {"files": files}


def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN,
        SERVICE_DOWNLOAD_INVOICE,
        _async_download,
        schema=SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
