"""Coordinator: backfill, idempotent statistics on real dates, polling policy, auth refusal and
DataDome block."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.totalenergies_pro.api import (
    Tokens,
    TotalEnergiesAuthError,
    TotalEnergiesBlockedError,
    TotalEnergiesTemporaryError,
)
from custom_components.totalenergies_pro.const import CONF_SPREAD, DOMAIN, PARIS, PDF_READER
from custom_components.totalenergies_pro.coordinator import (
    FetchState,
    async_clear_auth_refused,
    make_store,
)
from custom_components.totalenergies_pro.statistics import day_start, statistic_id

from .common import (
    ENTRY_DATA,
    NOW,
    PCE,
    PRM,
    TODAY,
    FakeClient,
    line,
    make_entry,
    move,
    setup,
    stats,
)

E_ENERGY = statistic_id(PRM, "billed_energy")
E_HPH = statistic_id(PRM, "billed_energy_hph")
E_TTC = statistic_id(PRM, "cost_incl_vat")
G_ENERGY = statistic_id(PCE, "billed_energy")
START = TODAY - timedelta(days=365)


def reauth_flows(hass: HomeAssistant) -> list[Any]:
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    return [f for f in flows if f["context"]["source"] == "reauth"]


def _day(row: dict[str, Any]) -> date:
    return datetime.fromtimestamp(row["start"], UTC).astimezone(PARIS).date()


async def test_first_setup_backfills_statistics_on_real_dates(
    hass: HomeAssistant, te: FakeClient
) -> None:
    entry = make_entry()
    await setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert te.calls[:3] == [
        ("sites",),
        ("invoices", START, TODAY),
        ("consumptions", START, TODAY),
    ]
    # Then the PDFs of the newest invoices, a few per fetch.
    assert [c[0] for c in te.calls[3:]] == ["pdf"] * 6
    # 12 invoices per energy issued in the last 12 months; electricity read on the 24th.
    rows = await stats(hass, E_ENERGY)
    assert _day(rows[0]) == date(2025, 2, 24)
    assert _day(rows[-1]) == date(2026, 2, 23)
    assert len(rows) == 365
    assert datetime.fromtimestamp(rows[0]["start"], UTC).hour == 23  # 00:00 Paris, winter
    assert rows[-1]["sum"] == pytest.approx(12 * 1300.0)
    assert rows[0]["state"] == pytest.approx(1300.0 / 28)  # Feb 24 → Mar 23, 2025: 28 days
    assert (await stats(hass, E_HPH))[-1]["sum"] == pytest.approx(12 * 1000.0)
    assert (await stats(hass, E_TTC))[-1]["sum"] == pytest.approx(12 * 240.0)
    assert (await stats(hass, statistic_id(PRM, "cost_excl_vat")))[-1]["sum"] == pytest.approx(
        12 * 200.0
    )
    assert (await stats(hass, statistic_id(PRM, "energy_cost")))[-1]["sum"] == pytest.approx(
        12 * 147.0
    )
    gas = await stats(hass, G_ENERGY)
    assert (_day(gas[0]), _day(gas[-1])) == (date(2025, 3, 1), date(2026, 2, 28))
    assert gas[-1]["sum"] == pytest.approx(12 * 5000.0)

    coordinator = entry.runtime_data
    summary = coordinator.data.points[PRM]
    assert summary.last_invoice is not None
    assert summary.last_invoice.date == date(2026, 3, 10)
    assert summary.average_price_incl_vat == pytest.approx(240 / 1300, abs=1e-5)
    assert coordinator.state.backfilled_years == 1
    assert coordinator.state.classes[PRM] == ["hch", "hph"]
    assert coordinator.state.first_day[PRM] == date(2025, 2, 24)


async def test_daily_reimport_is_idempotent_and_applies_corrections(
    hass: HomeAssistant, te: FakeClient
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    before = await stats(hass, E_ENERGY)

    te.calls.clear()
    await entry.runtime_data.async_refresh()
    assert te.calls[1] == ("invoices", START, TODAY)  # 12 months: one call per search
    after = await stats(hass, E_ENERGY)
    assert [r["sum"] for r in after] == pytest.approx([r["sum"] for r in before])

    # TotalEnergies re-bills the last electricity period: 100 kWh more on HPH.
    last = te.lines[-3]
    assert (last.point, last.time_class) == (PRM, "HPH")
    te.lines[-3] = replace(last, energy_kwh=1100.0)
    await entry.runtime_data.async_refresh()
    rows = await stats(hass, E_ENERGY)
    assert len(rows) == len(before)
    assert rows[-1]["sum"] == pytest.approx(before[-1]["sum"] + 100.0)
    unchanged = [r for r in rows if _day(r) < last.start]  # type: ignore[operator]
    assert [r["sum"] for r in unchanged] == pytest.approx(
        [r["sum"] for r in before[: len(unchanged)]]
    )


async def test_a_line_that_disappears_is_zeroed(hass: HomeAssistant, te: FakeClient) -> None:
    entry = make_entry()
    await setup(hass, entry)
    await stats(hass, E_ENERGY)  # the recorder has written the first import
    removed = te.lines.pop(-3)  # the last HPH line
    await entry.runtime_data.async_refresh()
    rows = await stats(hass, E_ENERGY)
    assert rows[-1]["sum"] == pytest.approx(12 * 1300.0 - removed.energy_kwh)  # type: ignore[operator]
    hph = await stats(hass, E_HPH)
    assert hph[-1]["state"] == 0.0


async def test_spread_option_change_reimports_everything(
    hass: HomeAssistant, te: FakeClient
) -> None:
    entry = make_entry(**{CONF_SPREAD: "period_end"})
    await setup(hass, entry)
    rows = await stats(hass, E_ENERGY)
    assert [_day(r) for r in rows][-2:] == [date(2026, 1, 23), date(2026, 2, 23)]
    assert rows[-1]["state"] == pytest.approx(1300.0)

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_SPREAD: "per_day"})
    await hass.async_block_till_done(wait_background_tasks=True)
    te.calls.clear()
    entry.runtime_data.state.last_attempt = None
    await entry.runtime_data.async_refresh()
    rows = await stats(hass, E_ENERGY)
    assert rows[-1]["sum"] == pytest.approx(12 * 1300.0)
    assert rows[-1]["state"] == pytest.approx(1300.0 / 31)  # Jan 24 → Feb 23, 2026
    assert entry.runtime_data.state.spread == "per_day"


async def test_history_lost_with_the_database_is_imported_again(
    hass: HomeAssistant, te: FakeClient
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    coordinator = entry.runtime_data
    coordinator.state.first_day[PRM] = date(2024, 6, 1)  # older than what the statistics hold
    te.calls.clear()
    await coordinator.async_refresh()
    assert te.calls[1] == ("invoices", START, TODAY)  # backfill depth (1 year in these tests)
    assert coordinator.state.first_day[PRM] == date(2025, 2, 24)


async def test_restart_shows_last_values_without_fetching(
    hass: HomeAssistant, te: FakeClient
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert te.calls.count(("sites",)) == 1  # fetched today already
    assert entry.runtime_data.data.points[PRM].last_invoice is not None


async def test_daily_schedule(
    hass: HomeAssistant, te: FakeClient, freezer: FrozenDateTimeFactory
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    te.calls.clear()
    await move(hass, freezer, NOW + timedelta(hours=8))  # 21:00: nothing
    assert te.calls == []
    await move(hass, freezer, NOW + timedelta(days=1, hours=1))  # next day after 11:30
    assert te.calls[0] == ("sites",)


async def test_session_of_the_config_flow_is_moved_to_the_store(
    hass: HomeAssistant, te: FakeClient
) -> None:
    tokens = Tokens("id-synth", "refresh-synth", NOW.timestamp() + 3600)
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**ENTRY_DATA, "tokens": tokens.as_dict()})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "tokens" not in entry.data
    assert te.kwargs["tokens"] == tokens

    # A new session handed by the client is saved with the state.
    renewed = Tokens("id-2", "refresh-2", NOW.timestamp() + 7200)
    te.kwargs["on_tokens"](renewed)
    await entry.runtime_data.async_refresh()
    saved = FetchState.from_dict(await make_store(hass, entry.entry_id).async_load())
    assert saved.tokens == renewed.as_dict()


async def test_auth_refusal_stops_everything_and_starts_reauth(
    hass: HomeAssistant, te: FakeClient, freezer: FrozenDateTimeFactory
) -> None:
    te.error = TotalEnergiesAuthError("refused", 200)
    entry = make_entry()
    await setup(hass, entry)

    assert te.calls == [("sites",)]
    coordinator = entry.runtime_data
    assert coordinator.state.auth_refused
    assert coordinator.state.tokens is None
    assert reauth_flows(hass)
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"auth_refused_{entry.entry_id}") is not None

    # No more call: not on schedule, not with the button, not after a restart.
    await move(hass, freezer, NOW + timedelta(days=2))
    with pytest.raises(HomeAssistantError, match="re-authenticate"):
        await coordinator.async_manual_refresh()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert te.calls == [("sites",)]

    # The reauth flow lifts the block.
    await async_clear_auth_refused(hass, entry.entry_id, None)
    assert issues.async_get_issue(DOMAIN, f"auth_refused_{entry.entry_id}") is None
    saved = FetchState.from_dict(await make_store(hass, entry.entry_id).async_load())
    assert not saved.auth_refused


async def test_datadome_block_waits_and_raises_an_issue_when_it_persists(
    hass: HomeAssistant, te: FakeClient, freezer: FrozenDateTimeFactory
) -> None:
    until = (NOW + timedelta(hours=20)).timestamp()
    te.error = TotalEnergiesBlockedError(until)
    entry = make_entry()
    await setup(hass, entry)
    coordinator = entry.runtime_data
    issues = ir.async_get(hass)
    assert coordinator.state.blocked_until == datetime.fromtimestamp(until, UTC)
    assert issues.async_get_issue(DOMAIN, f"datadome_blocked_{entry.entry_id}") is None
    assert te.calls == [("sites",)]

    with pytest.raises(HomeAssistantError, match="anti-bot"):
        await coordinator.async_manual_refresh()
    await hass.config_entries.async_reload(entry.entry_id)  # no fetch at startup while blocked
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert te.calls == [("sites",)]

    # Next day, blocked again: Repairs issue.
    te.error = TotalEnergiesBlockedError((NOW + timedelta(days=2)).timestamp())
    await move(hass, freezer, NOW + timedelta(days=1))
    assert coordinator.state.blocked_count == 2
    assert issues.async_get_issue(DOMAIN, f"datadome_blocked_{entry.entry_id}") is not None

    # A successful fetch clears it.
    te.error = None
    await move(hass, freezer, NOW + timedelta(days=2, hours=1))
    assert coordinator.state.blocked_count == 0
    assert issues.async_get_issue(DOMAIN, f"datadome_blocked_{entry.entry_id}") is None


async def test_temporary_error_is_recorded_without_numbers(
    hass: HomeAssistant, te: FakeClient
) -> None:
    te.error = TotalEnergiesTemporaryError(f"HTTP 500 on /x/{PRM}")
    entry = make_entry()
    await setup(hass, entry)
    state = entry.runtime_data.state
    assert state.last_result == "failed"
    assert state.last_error == "HTTP 500 on /x/<n>"
    assert not state.auth_refused
    assert not reauth_flows(hass)


async def test_missing_site_list_keeps_the_contracts_and_imports_the_rest(
    hass: HomeAssistant, te: FakeClient
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    coordinator = entry.runtime_data
    contract = coordinator.data.points[PRM].contract
    assert contract is not None
    before = await stats(hass, E_ENERGY)

    te.sites_error = TotalEnergiesTemporaryError("no delivery point list on /web-methods/sites")
    await coordinator.async_refresh()
    assert coordinator.state.last_result == "ok"
    assert coordinator.data.points[PRM].contract == contract
    assert coordinator.data.points[PRM].last_invoice is not None
    assert len(await stats(hass, E_ENERGY)) == len(before)

    # A DataDome block on the same route still stops the fetch.
    te.sites_error = TotalEnergiesBlockedError(0)
    await coordinator.async_refresh()
    assert coordinator.state.last_result != "ok"


async def test_unexpected_error_is_recorded(hass: HomeAssistant, te: FakeClient) -> None:
    te.error = KeyError("boom")
    entry = make_entry()
    await setup(hass, entry)
    assert entry.runtime_data.state.last_error == "unexpected KeyError: 'boom'"


async def test_manual_refresh_is_rate_limited(hass: HomeAssistant, te: FakeClient) -> None:
    entry = make_entry()
    await setup(hass, entry)
    with pytest.raises(HomeAssistantError, match="few minutes"):
        await entry.runtime_data.async_manual_refresh()
    entry.runtime_data.state.last_attempt = NOW - timedelta(minutes=10)
    te.calls.clear()
    await entry.runtime_data.async_manual_refresh()
    assert te.calls[0] == ("sites",)


async def test_interrupted_fetch_is_retried_soon(hass: HomeAssistant, te: FakeClient) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    store = make_store(hass, entry.entry_id)
    await store.async_save(
        FetchState(last_attempt=NOW - timedelta(minutes=20), last_result="running").as_dict()
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert te.calls[0] == ("sites",)


async def test_extra_line_for_an_unselected_point_is_ignored(
    hass: HomeAssistant, te: FakeClient
) -> None:
    te.lines.append(
        line("100000009999", "99999999999999", date(2026, 1, 1), date(2026, 1, 31), "HPH", 1, 1)
    )
    entry = make_entry()
    await setup(hass, entry)
    assert await stats(hass, statistic_id("99999999999999", "billed_energy")) == []
    assert day_start(date(2026, 1, 1)).hour == 23


async def test_invoice_pdfs_are_read_a_few_per_fetch_and_once(
    hass: HomeAssistant, te: FakeClient
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    coordinator = entry.runtime_data
    first = [c[1] for c in te.calls if c[0] == "pdf"]
    assert len(first) == 6 and first[0].startswith(("1", "2"))
    for _ in range(3):
        await coordinator.async_refresh()
    pdfs = [c[1] for c in te.calls if c[0] == "pdf"]
    assert len(pdfs) == len(set(pdfs)) == 24  # 12 per energy over 12 months, each read once
    te.calls.clear()
    await coordinator.async_refresh()
    assert not [c for c in te.calls if c[0] == "pdf"]

    s = coordinator.data.points[PRM].pdf
    assert s is not None and s.invoices == 12
    assert s.last is not None and s.last.invoice_date == date(2026, 3, 10)
    assert s.max_reached_kva == 30.0 + 12  # December
    assert s.reached_kva_by_class == {"hph": 42.0, "hch": 20.0}
    assert s.overrun_hours == 1.0
    gas = coordinator.data.points[PCE].pdf
    assert gas is not None and gas.last is not None
    assert gas.last.references["car_kwh"] == 150000.0
    # Kept across restarts (private storage), without numbers in the summaries.
    saved = FetchState.from_dict(await make_store(hass, entry.entry_id).async_load())
    assert len(saved.pdf_details) == 24
    assert saved.summaries[PRM]["pdf"]["max_reached_kva"] == 42.0

    # A result kept by an older reading is read again, a few per fetch.
    for record in list(coordinator.state.pdf_details.values())[:2]:
        record.pop("reader")
    te.calls.clear()
    await coordinator.async_refresh()
    assert len([c for c in te.calls if c[0] == "pdf"]) == 2
    assert all(r["reader"] == PDF_READER for r in coordinator.state.pdf_details.values())


async def test_unreadable_pdf_is_left_aside_after_three_attempts(
    hass: HomeAssistant, te: FakeClient
) -> None:
    newest = max(te.invoices, key=lambda i: i.date or date.min)
    assert newest.number is not None
    te.unreadable.add(newest.number)
    entry = make_entry()
    await setup(hass, entry)
    coordinator = entry.runtime_data
    for _ in range(5):
        await coordinator.async_refresh()
    tries = [c for c in te.calls if c == ("pdf", newest.number)]
    assert len(tries) == 3
    assert coordinator.state.last_result == "ok"
    assert coordinator.state.pdf_failures[newest.number] == 3


async def test_pdf_download_error_stops_the_reading_not_the_fetch(
    hass: HomeAssistant, te: FakeClient
) -> None:
    calls = 0

    async def flaky(number: str) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TotalEnergiesTemporaryError(f"HTTP 500 on /download/{number}")
        te.last_pdf = number
        return b"%PDF"

    te.async_download_invoice = flaky  # type: ignore[method-assign]
    entry = make_entry()
    await setup(hass, entry)
    coordinator = entry.runtime_data
    assert calls == 2
    assert coordinator.state.last_result == "ok"
    assert len(coordinator.state.pdf_details) == 1


async def test_pdf_reading_can_be_turned_off(hass: HomeAssistant, te: FakeClient) -> None:
    entry = make_entry(read_pdfs=False)
    await setup(hass, entry)
    assert not [c for c in te.calls if c[0] == "pdf"]
    assert entry.runtime_data.data.points[PRM].pdf is None


async def test_blocked_pdf_download_is_a_block(hass: HomeAssistant, te: FakeClient) -> None:
    async def blocked(number: str) -> bytes:
        raise TotalEnergiesBlockedError(0)

    te.async_download_invoice = blocked  # type: ignore[method-assign]
    entry = make_entry()
    await setup(hass, entry)
    assert entry.runtime_data.state.last_result == "blocked"
