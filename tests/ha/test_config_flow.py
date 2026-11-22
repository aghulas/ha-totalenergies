"""Config flow, reauth and options. One sign-in attempt per form submission, never more."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.totalenergies_pro.api import (
    ConsumptionSearch,
    Tokens,
    TotalEnergiesAuthError,
    TotalEnergiesBlockedError,
    TotalEnergiesTemporaryError,
)
from custom_components.totalenergies_pro.config_flow import point_labels
from custom_components.totalenergies_pro.const import CONF_POINTS, DOMAIN
from custom_components.totalenergies_pro.coordinator import FetchState, make_store

from .common import PASSWORD, PCE, PRM, USERNAME, line, make_entry, site

USER = {"username": " Compte@Exemple.invalid ", "password": PASSWORD}
TOKENS = Tokens("id-synth", "refresh-synth", 2_000_000_000.0)


class FakeSignIn:
    """Stands in for TotalEnergiesClient in the flow; counts the sign-in attempts."""

    def __init__(self) -> None:
        self.logins: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.sites = [
            site(PRM, "electricity", "C4", {"hph": 42.0}),
            site(PCE, "gas", "T3", {}),
        ]
        self.tokens: Tokens | None = TOKENS
        self.sites_error: Exception | None = None
        self.lines: list[Any] = []
        self.searches: list[tuple[date, date]] = []

    def build(self, _session: Any, username: str, password: str) -> FakeSignIn:
        self._pending = (username, password)
        return self

    async def async_login(self) -> None:
        self.logins.append(self._pending)
        if self.error is not None:
            raise self.error

    async def async_get_sites(self) -> list[Any]:
        if self.sites_error is not None:
            raise self.sites_error
        return self.sites

    async def async_get_consumptions(self, start: date, end: date) -> ConsumptionSearch:
        self.searches.append((start, end))
        return ConsumptionSearch(self.lines, True)


@pytest.fixture
def sign_in() -> Iterator[FakeSignIn]:
    fake = FakeSignIn()
    with (
        patch(
            "custom_components.totalenergies_pro.config_flow.TotalEnergiesClient",
            side_effect=fake.build,
        ),
        patch("custom_components.totalenergies_pro.async_setup_entry", return_value=True),
    ):
        yield fake


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return str(result["flow_id"])


async def test_user_flow_creates_the_entry(hass: HomeAssistant, sign_in: FakeSignIn) -> None:
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "points"
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_POINTS: [PRM]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USER["username"].strip()
    data = result["data"]
    assert data["username"] == USER["username"].strip()
    assert data[CONF_POINTS] == {
        PRM: {"energy": "electricity", "segment": "C4", "label": "Electricity C4"}
    }
    assert data["tokens"] == TOKENS.as_dict()
    assert result["result"].unique_id == USERNAME
    assert sign_in.logins == [(USER["username"].strip(), PASSWORD)]


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (TotalEnergiesAuthError("refused", 200), "invalid_auth"),
        (TotalEnergiesTemporaryError("HTTP 500"), "cannot_connect"),
        (TotalEnergiesBlockedError(0), "blocked"),
    ],
)
async def test_sign_in_errors_are_shown_and_not_retried(
    hass: HomeAssistant, sign_in: FakeSignIn, error: Exception, key: str
) -> None:
    sign_in.error = error
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    assert len(sign_in.logins) == 1


async def test_points_come_from_the_bills_when_the_site_list_is_missing(
    hass: HomeAssistant, sign_in: FakeSignIn
) -> None:
    sign_in.sites_error = TotalEnergiesTemporaryError("no delivery point list")
    jan1, jan31 = date(2026, 1, 1), date(2026, 1, 31)
    sign_in.lines = [
        line("1", PRM, jan1, jan31, "HPH", 10.0, 1.0),
        line("2", PCE, jan1, jan31, None, 10.0, 1.0),
    ]
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER)
    assert result["step_id"] == "points"
    assert len(sign_in.searches) == 1
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_POINTS: [PRM, PCE]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POINTS] == {
        PRM: {"energy": "electricity", "segment": None, "label": "Electricity"},
        PCE: {"energy": "gas", "segment": None, "label": "Gas"},
    }
    assert len(sign_in.logins) == 1


async def test_blocked_site_list_is_not_worked_around(
    hass: HomeAssistant, sign_in: FakeSignIn
) -> None:
    sign_in.sites_error = TotalEnergiesBlockedError(0)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER)
    assert result["errors"] == {"base": "blocked"}
    assert sign_in.searches == []


async def test_no_point_selected(hass: HomeAssistant, sign_in: FakeSignIn) -> None:
    flow_id = await _start(hass)
    await hass.config_entries.flow.async_configure(flow_id, USER)
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_POINTS: []})
    assert result["errors"] == {"base": "no_point_selected"}


async def test_no_points(hass: HomeAssistant, sign_in: FakeSignIn) -> None:
    sign_in.sites = []
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_points"


async def test_already_configured(hass: HomeAssistant, sign_in: FakeSignIn) -> None:
    make_entry().add_to_hass(hass)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert sign_in.logins == []


async def test_reauth_lifts_the_block(hass: HomeAssistant, sign_in: FakeSignIn) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    store = make_store(hass, entry.entry_id)
    await store.async_save(FetchState(auth_refused=True, auth_reason="refused").as_dict())

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    sign_in.error = TotalEnergiesAuthError("refused", 200)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "encore-faux"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert FetchState.from_dict(await store.async_load()).auth_refused

    sign_in.error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "nouveau"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "nouveau"
    await hass.async_block_till_done()
    saved = FetchState.from_dict(await make_store(hass, entry.entry_id).async_load())
    assert not saved.auth_refused
    assert saved.tokens == TOKENS.as_dict()
    assert sign_in.logins == [(USERNAME, "encore-faux"), (USERNAME, "nouveau")]


async def test_options_flow(hass: HomeAssistant, sign_in: FakeSignIn) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "history_years": 5,
            "fetch_hour": 9,
            "spread": "period_end",
            "pdf_folder": " /media/totalenergies ",
            "read_pdfs": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "history_years": 5,
        "fetch_hour": 9,
        "spread": "period_end",
        "pdf_folder": "/media/totalenergies",
        "read_pdfs": False,
    }


def test_point_labels_disambiguate() -> None:
    a = site("00000000001111", "electricity", "C4", {})
    b = site("00000000002222", "electricity", "C4", {})
    c = site(PCE, "gas", "T3", {})
    assert point_labels([a, b, c]) == {
        "00000000001111": "Electricity C4 …1111",
        "00000000002222": "Electricity C4 …2222",
        PCE: "Gas T3",
    }
    assert date(2025, 1, 1) == a.contract.start
