"""CLAUDE.md rules 1 and 2: a refused password is sent exactly once, then nothing at all; a
DataDome block stops every call without retry."""

from __future__ import annotations

import logging
from collections.abc import Coroutine
from datetime import date
from typing import Any

import aiohttp
import pytest

from custom_components.totalenergies_pro.api import (
    Tokens,
    TotalEnergiesAuthError,
    TotalEnergiesBlockedError,
    TotalEnergiesClient,
    TotalEnergiesTemporaryError,
)

from .conftest import INVOICE_1, NOW, PASSWORD, USERNAME, Clock, FakeTotalEnergies, make_client

Fake = tuple[FakeTotalEnergies, str]


def _entry_points(client: TotalEnergiesClient) -> list[Coroutine[Any, Any, Any]]:
    return [
        client.async_get_accounts(),
        client.async_get_sites(),
        client.async_get_invoices(date(2026, 1, 1), date(2026, 3, 1)),
        client.async_get_consumptions(date(2026, 1, 1), date(2026, 3, 1)),
        client.async_download_invoice(INVOICE_1),
        client.async_login(),
    ]


async def _assert_auth_blocked(client: TotalEnergiesClient, fake: FakeTotalEnergies) -> None:
    """Every entry point now fails without a single HTTP request."""
    before = len(fake.calls)
    for call in _entry_points(client):
        with pytest.raises(TotalEnergiesAuthError) as blocked:
            await call
        assert blocked.value.reason == "blocked"
    assert len(fake.calls) == before


async def test_refused_password_200_empty_list_is_sent_exactly_once(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    state, _ = fake
    client = make_client(fake, http, clock, password="ancien-mot-de-passe")

    with pytest.raises(TotalEnergiesAuthError) as err:
        await client.async_get_accounts()

    assert err.value.reason == "refused"
    assert err.value.status == 200
    assert state.calls == [("POST", "/login")]
    assert client.auth_error is err.value
    assert client.tokens is None

    await _assert_auth_blocked(client, state)

    # Hours later, still nothing: only a new client (reauth) may talk again.
    clock.now += 10 * 86400
    await _assert_auth_blocked(client, state)
    assert state.calls == [("POST", "/login")]

    for text in (str(err.value), caplog.text):
        assert "ancien-mot-de-passe" not in text
        assert USERNAME not in text


async def test_refused_password_with_a_4xx_blocks_too(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    state.login_mode = "refused_401"
    client = make_client(fake, http, clock, password="faux")
    with pytest.raises(TotalEnergiesAuthError) as err:
        await client.async_login()
    assert (err.value.reason, err.value.status) == ("refused", 401)
    await _assert_auth_blocked(client, state)
    assert state.count("/login") == 1


async def test_refused_refresh_then_refused_password(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    state.refresh_mode = "refused"
    old = Tokens("expired", "refresh-old", NOW - 10)
    client = make_client(fake, http, clock, password="faux", tokens=old)
    with pytest.raises(TotalEnergiesAuthError):
        await client.async_get_sites()
    assert state.calls == [("POST", "/refresh-token"), ("POST", "/login")]
    await _assert_auth_blocked(client, state)


async def test_refused_refresh_without_password_asks_for_reauth(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    state.refresh_mode = "refused"
    client = make_client(
        fake, http, clock, password=None, tokens=Tokens("expired", "refresh-old", NOW - 10)
    )
    with pytest.raises(TotalEnergiesAuthError) as err:
        await client.async_get_sites()
    assert err.value.reason == "refresh_expired"
    assert state.calls == [("POST", "/refresh-token")]
    await _assert_auth_blocked(client, state)


async def test_login_server_error_is_temporary_and_not_resent_soon(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    state.login_mode = "server_error"
    client = make_client(fake, http, clock)
    with pytest.raises(TotalEnergiesTemporaryError):
        await client.async_get_accounts()
    assert client.auth_error is None

    # Within the login interval the password is not sent again, whatever the caller does.
    state.login_mode = "normal"
    clock.now += 600
    with pytest.raises(TotalEnergiesTemporaryError, match="backing off"):
        await client.async_get_accounts()
    assert state.count("/login") == 1

    clock.now += 3600
    await client.async_get_accounts()
    assert state.count("/login") == 2


async def test_refresh_server_error_does_not_fall_back_to_the_password(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    state.refresh_mode = "server_error"
    client = make_client(fake, http, clock, tokens=Tokens("expired", "refresh-1", NOW - 10))
    with pytest.raises(TotalEnergiesTemporaryError):
        await client.async_get_accounts()
    assert state.calls == [("POST", "/refresh-token")]
    assert client.tokens is not None  # kept for the next attempt


async def test_datadome_on_a_data_call_stops_everything_without_retry(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    state, _ = fake
    client = make_client(fake, http, clock)
    await client.async_get_accounts()
    state.api_mode = "datadome"
    calls = len(state.calls)

    with pytest.raises(TotalEnergiesBlockedError) as err:
        await client.async_get_sites()
    assert len(state.calls) == calls + 1  # no retry
    assert err.value.retry_after == pytest.approx(NOW + 20 * 3600)
    assert client.blocked_until == err.value.retry_after
    assert client.auth_error is None
    assert "DataDome" in caplog.text

    for call in _entry_points(client):
        with pytest.raises(TotalEnergiesBlockedError):
            await call
    assert len(state.calls) == calls + 1

    # The next day, calls resume (one at a time; the block is not an auth problem).
    state.api_mode = "normal"
    clock.now += 21 * 3600
    assert client.blocked_until is None
    await client.async_get_sites()


async def test_datadome_on_login_is_not_a_refusal(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock
) -> None:
    state, _ = fake
    state.login_mode = "datadome"
    client = make_client(fake, http, clock)
    with pytest.raises(TotalEnergiesBlockedError):
        await client.async_get_accounts()
    assert client.auth_error is None
    with pytest.raises(TotalEnergiesBlockedError):
        await client.async_login()
    assert state.count("/login") == 1


async def test_password_never_logged_on_success(
    fake: Fake, http: aiohttp.ClientSession, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = make_client(fake, http, clock)
    await client.async_get_accounts()
    assert client.tokens is not None
    for secret in (PASSWORD, USERNAME, client.tokens.id_token, client.tokens.refresh_token):
        assert secret is not None
        assert secret not in caplog.text
        assert secret not in repr(client.tokens)
