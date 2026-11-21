"""TotalEnergies Pro (pro.totalenergies.fr) — invoices, billed consumption, costs and prices.

Read-only, and built never to lock the TotalEnergies account: see coordinator.py and
api/client.py.
"""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import Tokens, TotalEnergiesClient
from .const import DOMAIN, ISSUE_AUTH_REFUSED
from .coordinator import (
    FetchState,
    TotalEnergiesConfigEntry,
    TotalEnergiesCoordinator,
    async_create_auth_issue,
    make_store,
)
from .services import async_setup_services

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]
CONF_TOKENS = "tokens"  # session of the config flow's sign-in, handed over once

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TotalEnergiesConfigEntry) -> bool:
    store = make_store(hass, entry.entry_id)
    state = FetchState.from_dict(await store.async_load())
    if state.auth_refused:
        # A previous refusal is still pending: nothing is sent to TotalEnergies until the user
        # re-authenticates (the reauth flow clears the flag).
        async_create_auth_issue(hass, entry)
        raise ConfigEntryAuthFailed("TotalEnergies refused the credentials; reauth needed")
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_AUTH_REFUSED}_{entry.entry_id}")

    if CONF_TOKENS in entry.data:
        # The sign-in of the config flow: reuse its session instead of sending the password
        # again, and keep it in the private store only.
        state.tokens = state.tokens or entry.data[CONF_TOKENS]
        await store.async_save(state.as_dict())
        hass.config_entries.async_update_entry(
            entry, data={k: v for k, v in entry.data.items() if k != CONF_TOKENS}
        )

    # A dedicated session: DataDome's cookie belongs to this client only.
    session = async_create_clientsession(hass)
    coordinator: TotalEnergiesCoordinator
    client = TotalEnergiesClient(
        session,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        tokens=Tokens.from_dict(state.tokens),
        on_tokens=lambda tokens: coordinator.on_tokens(tokens),
    )
    coordinator = TotalEnergiesCoordinator(hass, entry, client, store, state)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TotalEnergiesConfigEntry) -> bool:
    # The schedule is cancelled by the entry's unload callbacks.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: TotalEnergiesConfigEntry) -> None:
    await make_store(hass, entry.entry_id).async_remove()
