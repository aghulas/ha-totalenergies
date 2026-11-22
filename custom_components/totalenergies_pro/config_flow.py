"""Config flow: sign in, pick the delivery points; reauth; options.

Every sign-in attempt here is triggered by the user submitting a form, and is made exactly once
(CLAUDE.md rule 1): an error is shown, and nothing is sent again until the user submits again.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util

from .api import (
    Site,
    Tokens,
    TotalEnergiesAuthError,
    TotalEnergiesBlockedError,
    TotalEnergiesClient,
    TotalEnergiesTemporaryError,
    sites_from_lines,
)
from .billing import SPREAD_PER_DAY, SPREAD_PERIOD_END
from .const import (
    CONF_FETCH_HOUR,
    CONF_HISTORY_YEARS,
    CONF_PDF_FOLDER,
    CONF_POINTS,
    CONF_READ_PDFS,
    CONF_SPREAD,
    DEFAULT_FETCH_HOUR,
    DEFAULT_HISTORY_YEARS,
    DEFAULT_PDF_FOLDER,
    DEFAULT_READ_PDFS,
    DOMAIN,
)
from .coordinator import async_clear_auth_refused

CONF_TOKENS = "tokens"
ENERGY_NAMES = {"electricity": "Electricity", "gas": "Gas"}

_PASSWORD = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): _PASSWORD,
    }
)

_LOGGER = logging.getLogger(__name__)

type SignInError = TotalEnergiesAuthError | TotalEnergiesTemporaryError


def _error_key(err: SignInError) -> str:
    if isinstance(err, TotalEnergiesBlockedError):
        return "blocked"
    if isinstance(err, TotalEnergiesTemporaryError):
        return "cannot_connect"
    return "invalid_auth"


def point_labels(sites: list[Site]) -> dict[str, str]:
    """Short labels for the devices: energy and segment, plus the end of the number when two
    points would share them."""
    base = {
        s.point: " ".join(x for x in (ENERGY_NAMES.get(s.energy or "", "Energy"), s.segment) if x)
        for s in sites
        if s.point
    }
    counts = Counter(base.values())
    return {p: f"{lbl} …{p[-4:]}" if counts[lbl] > 1 else lbl for p, lbl in base.items()}


class TotalEnergiesConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._sites: list[Site] = []
        self._tokens: Tokens | None = None

    async def _async_sign_in(
        self, username: str, password: str, *, sites: bool
    ) -> tuple[Tokens | None, list[Site]]:
        """One sign-in attempt with a throw-away session; optionally list the points."""
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            client = TotalEnergiesClient(session, username, password)
            await client.async_login()
            found = await self._async_points(client) if sites else []
            return client.tokens, found
        finally:
            session.detach()  # HA owns the connector; this only drops our cookie jar

    @staticmethod
    async def _async_points(client: TotalEnergiesClient) -> list[Site]:
        """The site list; when TotalEnergies withholds it, the points billed in the last year."""
        try:
            return await client.async_get_sites()
        except TotalEnergiesBlockedError:
            raise
        except TotalEnergiesTemporaryError as err:
            _LOGGER.warning("TotalEnergies site list unavailable (%s): using billed points", err)
        today = dt_util.now().date()
        search = await client.async_get_consumptions(today - timedelta(days=365), today)
        return sites_from_lines(search.lines)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            try:
                self._tokens, self._sites = await self._async_sign_in(
                    username, user_input[CONF_PASSWORD], sites=True
                )
            except (TotalEnergiesAuthError, TotalEnergiesTemporaryError) as err:
                errors["base"] = _error_key(err)
            else:
                self._sites = [s for s in self._sites if s.point and s.energy]
                if not self._sites:
                    return self.async_abort(reason="no_points")
                self._credentials = {
                    CONF_USERNAME: username,
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                return await self.async_step_points()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _USER_SCHEMA, {CONF_USERNAME: (user_input or {}).get(CONF_USERNAME)}
            ),
            errors=errors,
        )

    async def async_step_points(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        labels = point_labels(self._sites)
        if user_input is not None:
            if user_input[CONF_POINTS]:
                return self._async_create(user_input[CONF_POINTS], labels)
            errors["base"] = "no_point_selected"
        choices: dict[str, str] = {}
        for s in sorted(self._sites, key=lambda s: (not s.contract.active, s.energy or "")):
            assert s.point is not None
            status = "" if s.contract.active else f" — {s.contract.status}"
            choices.setdefault(s.point, f"{labels[s.point]} ({s.point}){status}")
        active = [s.point for s in self._sites if s.contract.active and s.point]
        return self.async_show_form(
            step_id="points",
            data_schema=vol.Schema(
                {vol.Required(CONF_POINTS, default=active): cv.multi_select(choices)}
            ),
            errors=errors,
        )

    def _async_create(self, points: list[str], labels: dict[str, str]) -> ConfigFlowResult:
        info: dict[str, dict[str, Any]] = {}
        for s in self._sites:
            if s.point in points and s.point not in info:
                info[s.point] = {"energy": s.energy, "segment": s.segment, "label": labels[s.point]}
        data: dict[str, Any] = {**self._credentials, CONF_POINTS: info}
        if self._tokens is not None:
            data[CONF_TOKENS] = self._tokens.as_dict()  # moved to the private store at setup
        return self.async_create_entry(title=self._credentials[CONF_USERNAME], data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                tokens, _ = await self._async_sign_in(
                    entry.data[CONF_USERNAME], user_input[CONF_PASSWORD], sites=False
                )
            except (TotalEnergiesAuthError, TotalEnergiesTemporaryError) as err:
                errors["base"] = _error_key(err)
            else:
                # TotalEnergies accepted the new credentials: lift the block, then reload.
                await async_clear_auth_refused(self.hass, entry.entry_id, tokens)
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry[Any]) -> TotalEnergiesOptionsFlow:
        return TotalEnergiesOptionsFlow()


def _number(low: float, high: float, unit: str) -> Any:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=low,
            max=high,
            step=1,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


class TotalEnergiesOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_HISTORY_YEARS: int(user_input[CONF_HISTORY_YEARS]),
                    CONF_FETCH_HOUR: int(user_input[CONF_FETCH_HOUR]),
                    CONF_SPREAD: str(user_input[CONF_SPREAD]),
                    CONF_PDF_FOLDER: str(user_input.get(CONF_PDF_FOLDER) or "").strip(),
                    CONF_READ_PDFS: bool(user_input.get(CONF_READ_PDFS, DEFAULT_READ_PDFS)),
                }
            )
        schema = vol.Schema(
            {
                vol.Required(CONF_HISTORY_YEARS): _number(1, 10, "years"),
                vol.Required(CONF_FETCH_HOUR): _number(0, 23, "h"),
                vol.Required(CONF_SPREAD): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[SPREAD_PER_DAY, SPREAD_PERIOD_END], translation_key=CONF_SPREAD
                    )
                ),
                vol.Optional(CONF_PDF_FOLDER): selector.TextSelector(),
                vol.Required(CONF_READ_PDFS): selector.BooleanSelector(),
            }
        )
        current = {
            CONF_HISTORY_YEARS: DEFAULT_HISTORY_YEARS,
            CONF_FETCH_HOUR: DEFAULT_FETCH_HOUR,
            CONF_SPREAD: SPREAD_PER_DAY,
            CONF_PDF_FOLDER: DEFAULT_PDF_FOLDER,
            CONF_READ_PDFS: DEFAULT_READ_PDFS,
            **self.config_entry.options,
        }
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(schema, current)
        )
