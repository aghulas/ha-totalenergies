"""Transport and data calls of the client: session handling, allow-list, windows, PDF."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import aiohttp
import pytest

from custom_components.totalenergies_pro.api import (
    Tokens,
    TotalEnergiesRouteNotAllowed,
    TotalEnergiesTemporaryError,
    check_call,
    is_datadome,
    public_path,
)
from custom_components.totalenergies_pro.api.client import jwt_exp, parse_expires_at

from .conftest import (
    INVOICE_1,
    INVOICE_2,
    NOW,
    PASSWORD,
    PDF,
    USERNAME,
    Clock,
    FakeTotalEnergies,
    invoice,
    jwt,
    line,
    make_client,
)

Fake = tuple[FakeTotalEnergies, str]


async def test_first_call_logs_in_once_then_reuses_the_token(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    seen: list[Tokens] = []
    client = make_client(fake, http, clock, on_tokens=seen.append)

    accounts = await client.async_get_accounts()
    sites = await client.async_get_sites()

    assert [a.name for a in accounts] == ["CLIENT SYNTH"]
    assert [s.energy for s in sites] == ["electricity"]
    assert state.calls == [
        ("POST", "/login"),
        ("GET", "/web-methods/comptes"),
        ("GET", "/web-methods/sites"),
    ]
    assert state.bodies[0] == ("/login", {"username": USERNAME, "password": PASSWORD})
    assert state.auth_headers[0] is None  # no bearer on the login
    assert state.auth_headers[1] == f"Bearer {seen[0].id_token}"
    assert len(seen) == 1
    assert seen[0].refresh_token == "refresh-1"
    assert seen[0].expires_at == NOW + 3600


async def test_stored_session_is_used_without_login(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    state.valid_tokens.add("stored")
    client = make_client(
        fake, http, clock, password=None, tokens=Tokens("stored", "refresh-0", NOW + 600)
    )
    await client.async_get_accounts()
    assert state.calls == [("GET", "/web-methods/comptes")]


async def test_expiring_token_is_refreshed_not_logged_in(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    seen: list[Tokens] = []
    client = make_client(
        fake,
        http,
        clock,
        tokens=Tokens("old", "refresh-0", NOW + 30),  # within the renewal margin
        on_tokens=seen.append,
    )
    await client.async_get_accounts()
    assert state.calls == [("POST", "/refresh-token"), ("GET", "/web-methods/comptes")]
    assert state.bodies[0] == ("/refresh-token", {"refreshToken": "refresh-0"})
    assert seen[-1].refresh_token == "refresh-1"  # rotated


async def test_refresh_without_new_refresh_token_keeps_the_old_one(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _ = fake
    original = state._session

    def without_refresh() -> dict[str, Any]:
        out = original()
        del out["refreshToken"]
        return out

    monkeypatch.setattr(state, "_session", without_refresh)
    client = make_client(fake, http, clock, tokens=Tokens("old", "refresh-0", NOW - 1))
    await client.async_get_accounts()
    assert client.tokens is not None
    assert client.tokens.refresh_token == "refresh-0"


async def test_401_renews_once_and_retries_once(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    client = make_client(fake, http, clock)
    await client.async_get_accounts()
    state.reject_next = 1
    await client.async_get_accounts()
    assert state.calls[1:] == [
        ("GET", "/web-methods/comptes"),
        ("GET", "/web-methods/comptes"),
        ("POST", "/refresh-token"),
        ("GET", "/web-methods/comptes"),
    ]


async def test_401_twice_is_temporary(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    client = make_client(fake, http, clock)
    await client.async_get_accounts()
    state.reject_next = 2
    with pytest.raises(TotalEnergiesTemporaryError, match="HTTP 401"):
        await client.async_get_accounts()
    assert state.count("/login") == 1
    assert client.auth_error is None


@pytest.mark.parametrize(("mode", "match"), [("server_error", "HTTP 500"), ("html", "not JSON")])
async def test_bad_answers_are_temporary(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock, mode: str, match: str
) -> None:
    state, _ = fake
    client = make_client(fake, http, clock)
    await client.async_login()
    state.api_mode = mode
    with pytest.raises(TotalEnergiesTemporaryError, match=match):
        await client.async_get_sites()


async def test_network_error_is_temporary(http: aiohttp.ClientSession, clock: Clock) -> None:
    client = make_client(({}, "http://127.0.0.1:9"), http, clock)  # type: ignore[arg-type]
    with pytest.raises(TotalEnergiesTemporaryError, match="request failed"):
        await client.async_login()


@pytest.mark.parametrize("style", ["missing", "ms"])
async def test_expiry_fallbacks(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock, style: str
) -> None:
    state, _ = fake
    state.expires_style = style
    client = make_client(fake, http, clock)
    await client.async_login()
    assert client.tokens is not None
    assert client.tokens.expires_at == NOW + 3600  # from the JWT exp, or from milliseconds


async def test_invoices_are_searched_in_12_month_windows_and_deduplicated(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    state.invoices = [invoice(INVOICE_1, "2025-02-10"), invoice(INVOICE_2, "2025-03-12", "02")]
    state.heavy_load = True
    client = make_client(fake, http, clock)

    result = await client.async_get_invoices(date(2024, 1, 15), date(2026, 1, 20))

    bodies = [b for p, b in state.bodies if p == "/web-methods/factures"]
    assert bodies == [
        {"dateDebutPeriode": "2024-01-15", "dateFinPeriode": "2025-01-14"},
        {"dateDebutPeriode": "2025-01-15", "dateFinPeriode": "2026-01-14"},
        {"dateDebutPeriode": "2026-01-15", "dateFinPeriode": "2026-01-20"},
    ]
    assert all(m == "POST" for m, p in state.calls if p == "/web-methods/factures")
    assert [i.number for i in result.invoices] == [INVOICE_2, INVOICE_1]  # newest first
    assert result.heavy_load is True
    assert result.invoices[0].energy == "gas"
    assert result.invoices[1].total_incl_vat == 120.5


async def test_consumptions_windows_dedup_and_partial(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    state.consumptions = [
        line(INVOICE_1, "2025-01-24", "2025-02-23", "HPH", "800", "96,00"),
        line(INVOICE_1, "2025-01-24", "2025-02-23", "HCH", "200,5", "18,04"),
    ]
    state.partial = True
    client = make_client(fake, http, clock)

    result = await client.async_get_consumptions(date(2024, 6, 1), date(2025, 12, 31))

    bodies = [b for p, b in state.bodies if p == "/web-methods/consommations"]
    assert bodies == [
        {"dateDebutRecherche": "2024-06-01", "dateFinRecherche": "2025-05-31"},
        {"dateDebutRecherche": "2025-06-01", "dateFinRecherche": "2025-12-31"},
    ]
    assert [(x.time_class, x.energy_kwh, x.amount_excl_tax) for x in result.lines] == [
        ("HCH", 200.5, 18.04),
        ("HPH", 800.0, 96.0),
    ]
    assert result.partial is True


async def test_download_invoice(fake: Fake, http: aiohttp.ClientSession, clock: Clock) -> None:
    state, _ = fake
    client = make_client(fake, http, clock)
    assert await client.async_download_invoice(f" {INVOICE_1} ") == PDF
    assert ("GET", f"/web-methods/download/facture/{INVOICE_1}") in state.calls

    state.pdf = b"<html>error</html>"
    with pytest.raises(TotalEnergiesTemporaryError, match="not a PDF"):
        await client.async_download_invoice(INVOICE_1)


@pytest.mark.parametrize("number", ["", "12345", "1000/../../x", "10000000000a", "1" * 21])
async def test_download_rejects_invalid_numbers_without_any_call(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock, number: str
) -> None:
    state, _ = fake
    client = make_client(fake, http, clock)
    with pytest.raises(ValueError):
        await client.async_download_invoice(number)
    assert state.calls == []


async def test_calls_are_spaced(fake: Fake, http: aiohttp.ClientSession, clock: Clock) -> None:
    client = make_client(fake, http, clock, min_interval=0.2)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await client.async_get_accounts()  # login + call
    await client.async_get_sites()
    assert loop.time() - start >= 0.4


def test_allow_list() -> None:
    check_call("POST", "/login")
    check_call("POST", "/web-methods/factures")
    check_call("GET", f"/web-methods/download/facture/{INVOICE_1}")
    for method, path in [
        ("GET", "/login"),
        ("POST", "/web-methods/sites"),
        ("POST", "/web-methods/paiement"),
        ("GET", "/web-methods/comptes/1"),
        ("PUT", "/web-methods/comptes"),
        ("GET", "/web-methods/download/facture/123"),
        ("GET", "/web-methods/download/facture/100000000001/x"),
    ]:
        with pytest.raises(TotalEnergiesRouteNotAllowed) as err:
            check_call(method, path)
        assert "100000000001" not in str(err.value)


def test_public_path_masks_numbers() -> None:
    assert public_path(f"/web-methods/download/facture/{INVOICE_1}") == (
        "/web-methods/download/facture/<n>"
    )
    assert public_path("/web-methods/factures?") == "/web-methods/factures"


def test_is_datadome() -> None:
    assert is_datadome({"X-DataDome": "protected"}, b"")
    assert is_datadome({"x-dd-b": "1"}, b"")
    assert is_datadome({"Set-Cookie": "datadome=abc; Path=/"}, b"")
    assert is_datadome({}, b'{"url":"https://geo.captcha-delivery.com/captcha/"}')
    assert not is_datadome({"Content-Type": "application/json"}, b'{"message":"Forbidden"}')


def test_expiry_parsing() -> None:
    assert parse_expires_at(1_800_000_000) == 1_800_000_000
    assert parse_expires_at(1_800_000_000_000) == 1_800_000_000
    assert parse_expires_at("1800000000") == 1_800_000_000
    assert parse_expires_at("2027-01-15T08:00:00Z") == 1_800_000_000
    assert parse_expires_at("2027-01-15T08:00:00") is None  # no time zone: not trusted
    for bad in (None, True, "", "demain", 0, -5, [1]):
        assert parse_expires_at(bad) is None
    assert jwt_exp(jwt(NOW)) == NOW
    for bad_token in ("", "abc", "a.!!!.c", "a.e30.c"):
        assert jwt_exp(bad_token) is None


def test_tokens_roundtrip_and_repr() -> None:
    tokens = Tokens("id-secret", "refresh-secret", NOW)
    assert Tokens.from_dict(tokens.as_dict()) == tokens
    assert "secret" not in repr(tokens)
    assert Tokens.from_dict(None) is None
    assert Tokens.from_dict({"id_token": 3}) is None
    partial = Tokens.from_dict({"id_token": "x", "refresh_token": 5, "expires_at": "?"})
    assert partial == Tokens("x", None, 0.0)
