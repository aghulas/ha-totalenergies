"""Read-only client of the TotalEnergies business customer API (api-b2b.totalenergies.fr).

Authentication (docs/api.md): `POST /login` → id_token (~1 h) + refresh token; `POST
/refresh-token` renews both. The id_token is sent as `Authorization: Bearer`.

Lockout safety (CLAUDE.md rule 1): `POST /login` answers **HTTP 200 with `[]`** when it refuses
the credentials. Any answer without a token is a refusal: the client is then *blocked* — every
further call raises TotalEnergiesAuthError("blocked") without any HTTP request. Only a new client,
built after a user action (reauth flow), talks to TotalEnergies again. The password is never sent
twice within `min_login_interval`, whatever happens.

Anti-bot (CLAUDE.md rule 2): a 403 carrying a DataDome header stops every call for
`datadome_backoff` seconds (TotalEnergiesBlockedError). Never retried, never bypassed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import aiohttp

from .const import (
    ALLOWED,
    API_BASE,
    DATADOME_BACKOFF,
    DEFAULT_TOKEN_LIFETIME,
    HEADERS,
    INVOICE_NUMBER_RE,
    MIN_INTERVAL,
    MIN_LOGIN_INTERVAL,
    PATH_ACCOUNTS,
    PATH_CONSUMPTIONS,
    PATH_INVOICE_PDF,
    PATH_INVOICES,
    PATH_LOGIN,
    PATH_REFRESH,
    PATH_SITES,
    REQUEST_TIMEOUT,
    TOKEN_MARGIN,
    WINDOW_MONTHS,
)
from .errors import (
    TotalEnergiesAuthError,
    TotalEnergiesBlockedError,
    TotalEnergiesRouteNotAllowed,
    TotalEnergiesTemporaryError,
)
from .models import (
    Account,
    ConsumptionLine,
    ConsumptionSearch,
    Invoice,
    InvoiceSearch,
    Site,
    merge_consumptions,
    merge_invoices,
    parse_accounts,
    parse_consumptions,
    parse_invoices,
    parse_sites,
    windows,
)

_LOGGER = logging.getLogger(__name__)

_DIGITS = re.compile(r"\d{4,}")


def check_call(method: str, path: str) -> None:
    """Raise unless (method, path) is in the read-only allow-list."""
    if not any(m == method and p.fullmatch(path) for m, p in ALLOWED):
        raise TotalEnergiesRouteNotAllowed(f"call not allowed: {method} {public_path(path)}")


def public_path(path: str) -> str:
    """A path that can be logged: numbers masked, query dropped."""
    return _DIGITS.sub("<n>", path.split("?")[0])


def is_datadome(headers: Mapping[str, str], body: bytes) -> bool:
    """A DataDome answer: a header named after it (x-datadome, x-datadome-cid…), its cookie,
    `x-dd-b`, or its captcha page."""
    for name, value in headers.items():
        lower = name.lower()
        if "datadome" in lower or lower == "x-dd-b":
            return True
        if lower == "set-cookie" and value.lower().startswith("datadome="):
            return True
    return b"captcha-delivery.com" in body


@dataclass(frozen=True, slots=True)
class Tokens:
    """The session. Persist it (config entry data) to avoid a password login at each start."""

    id_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: float  # epoch seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Tokens | None:
        if not isinstance(data, dict) or not isinstance(data.get("id_token"), str):
            return None
        refresh = data.get("refresh_token")
        expires = data.get("expires_at")
        return cls(
            id_token=data["id_token"],
            refresh_token=refresh if isinstance(refresh, str) else None,
            expires_at=float(expires) if isinstance(expires, int | float) else 0.0,
        )


def jwt_exp(token: str) -> float | None:
    """The `exp` claim of a JWT, without verifying it (only used to schedule the renewal)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
    except (IndexError, KeyError, ValueError, TypeError):
        return None
    return float(exp) if isinstance(exp, int | float) else None


def parse_expires_at(value: Any) -> float | None:
    """`expiresAt` as epoch seconds: seconds or milliseconds, number or string, or ISO 8601."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            value = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            return parsed.timestamp() if parsed.tzinfo else None
    if not isinstance(value, int | float) or value <= 0:
        return None
    return float(value) / 1000 if value > 1e12 else float(value)


class TotalEnergiesClient:
    """One TotalEnergies account. Calls are serialized and spaced by at least `min_interval`.

    `password` may be None: the client then lives on the refresh token alone and reports
    "refresh_expired" when it is refused. `on_tokens` receives every new session (to persist it).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str | None,
        *,
        tokens: Tokens | None = None,
        on_tokens: Callable[[Tokens], None] | None = None,
        base_url: str = API_BASE,
        min_interval: float = MIN_INTERVAL,
        min_login_interval: float = MIN_LOGIN_INTERVAL,
        datadome_backoff: float = DATADOME_BACKOFF,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._tokens = tokens
        self._on_tokens = on_tokens
        self._base = base_url.rstrip("/")
        self._min_interval = min_interval
        self._min_login_interval = min_login_interval
        self._datadome_backoff = datadome_backoff
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_call = float("-inf")
        self._last_login = float("-inf")
        self._auth_error: TotalEnergiesAuthError | None = None
        self._blocked_until = 0.0

    # ---- state ----------------------------------------------------------------------------

    @property
    def tokens(self) -> Tokens | None:
        return self._tokens

    @property
    def auth_error(self) -> TotalEnergiesAuthError | None:
        """The refusal that blocked this client, if any."""
        return self._auth_error

    @property
    def blocked_until(self) -> float | None:
        """End of the DataDome back-off (epoch seconds), if one is running."""
        return self._blocked_until if self._blocked_until > self._clock() else None

    def _raise_if_blocked(self) -> None:
        if self._auth_error is not None:
            raise TotalEnergiesAuthError("blocked", self._auth_error.status) from self._auth_error
        if self._blocked_until > self._clock():
            raise TotalEnergiesBlockedError(self._blocked_until)

    # ---- transport ------------------------------------------------------------------------

    async def _request(
        self, method: str, path: str, body: Any = None, *, auth: bool = True
    ) -> tuple[int, bytes]:
        """One HTTP call. Returns (status, raw body). Raises on DataDome and network errors."""
        check_call(method, path)
        self._raise_if_blocked()
        loop = asyncio.get_running_loop()
        wait = self._last_call + self._min_interval - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        headers = dict(HEADERS)
        if auth:
            assert self._tokens is not None
            headers["Authorization"] = f"Bearer {self._tokens.id_token}"
        try:
            async with self._session.request(
                method,
                self._base + path,
                json=body,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                status, raw = resp.status, await resp.read()
                datadome = status == 403 and is_datadome(resp.headers, raw)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TotalEnergiesTemporaryError(
                f"request failed ({type(err).__name__}) on {public_path(path)}"
            ) from err
        finally:
            self._last_call = loop.time()
        if datadome:
            self._blocked_until = self._clock() + self._datadome_backoff
            _LOGGER.warning(
                "TotalEnergies anti-bot protection (DataDome) blocked %s: no call for %.0f h",
                public_path(path),
                self._datadome_backoff / 3600,
            )
            raise TotalEnergiesBlockedError(self._blocked_until)
        return status, raw

    @staticmethod
    def _json(raw: bytes) -> Any:
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # ---- authentication -------------------------------------------------------------------

    def _store(self, data: dict[str, Any]) -> None:
        token = data["token"]
        expires = (
            parse_expires_at(data.get("expiresAt"))
            or jwt_exp(token)
            or self._clock() + DEFAULT_TOKEN_LIFETIME
        )
        refresh = data.get("refreshToken")
        if not isinstance(refresh, str) or not refresh:
            refresh = self._tokens.refresh_token if self._tokens else None
        self._tokens = Tokens(token, refresh, expires)
        if self._on_tokens is not None:
            self._on_tokens(self._tokens)

    @staticmethod
    def _has_token(data: Any) -> bool:
        return isinstance(data, dict) and isinstance(data.get("token"), str) and bool(data["token"])

    def _token_valid(self) -> bool:
        return self._tokens is not None and (self._tokens.expires_at - TOKEN_MARGIN > self._clock())

    async def _refresh(self) -> bool:
        """Renew the session with the refresh token. False when it is refused (or absent)."""
        refresh = self._tokens.refresh_token if self._tokens else None
        if not refresh:
            return False
        status, raw = await self._request(
            "POST", PATH_REFRESH, {"refreshToken": refresh}, auth=False
        )
        data = self._json(raw)
        if status == 200 and self._has_token(data):
            self._store(data)
            _LOGGER.debug("TotalEnergies session refreshed")
            return True
        if status >= 500 or status in (301, 302, 303, 307, 308, 429):
            raise TotalEnergiesTemporaryError(f"session refresh failed (HTTP {status})")
        _LOGGER.debug("TotalEnergies refresh token refused (HTTP %s)", status)
        self._tokens = None
        return False

    async def _password_login(self) -> None:
        """ONE `POST /login`. Any answer without a token, 200 included, is a refusal."""
        if self._password is None:
            self._auth_error = TotalEnergiesAuthError("refresh_expired")
            _LOGGER.warning("TotalEnergies session expired and no password stored: polling stopped")
            raise self._auth_error
        now = self._clock()
        if now - self._last_login < self._min_login_interval:
            raise TotalEnergiesTemporaryError("password login shortly after another: backing off")
        self._last_login = now
        status, raw = await self._request(
            "POST",
            PATH_LOGIN,
            {"username": self._username, "password": self._password},
            auth=False,
        )
        data = self._json(raw)
        if status == 200 and self._has_token(data):
            self._store(data)
            _LOGGER.debug("TotalEnergies password login succeeded")
            return
        if status >= 500 or status in (301, 302, 303, 307, 308, 429):
            # The password was not judged; the login interval still prevents a quick resend.
            raise TotalEnergiesTemporaryError(f"login failed (HTTP {status})")
        self._tokens = None
        self._auth_error = TotalEnergiesAuthError("refused", status)
        _LOGGER.warning("TotalEnergies refused the credentials (HTTP %s): polling stopped", status)
        raise self._auth_error

    async def _ensure_token(self, *, force: bool = False) -> None:
        if not force and self._token_valid():
            return
        if await self._refresh():
            return
        await self._password_login()

    async def async_login(self) -> None:
        """Log in with the password now (config flow, reauth). One attempt; a refusal blocks."""
        async with self._lock:
            self._raise_if_blocked()
            await self._password_login()

    # ---- authenticated calls --------------------------------------------------------------

    async def _call(self, method: str, path: str, body: Any = None) -> bytes:
        check_call(method, path)
        async with self._lock:
            self._raise_if_blocked()
            await self._ensure_token()
            status, raw = await self._request(method, path, body)
            if status == 401:
                _LOGGER.debug("TotalEnergies token rejected: renewing it once")
                await self._ensure_token(force=True)
                status, raw = await self._request(method, path, body)
            if status >= 400 or status in (301, 302, 303, 307, 308):
                raise TotalEnergiesTemporaryError(f"HTTP {status} on {public_path(path)}")
            return raw

    async def _call_json(self, method: str, path: str, body: Any = None) -> Any:
        data = self._json(await self._call(method, path, body))
        if data is None:
            raise TotalEnergiesTemporaryError(f"answer is not JSON on {public_path(path)}")
        return data

    # ---- data -----------------------------------------------------------------------------

    async def async_get_accounts(self) -> list[Account]:
        """Billing accounts."""
        return parse_accounts(await self._call_json("GET", PATH_ACCOUNTS))

    async def async_get_sites(self) -> list[Site]:
        """Delivery points and their contracts (active and terminated)."""
        return parse_sites(await self._call_json("GET", PATH_SITES))

    async def async_get_invoices(self, start: date, end: date) -> InvoiceSearch:
        """Invoices dated within [start, end], searched in windows of 12 months."""
        batches: list[list[Invoice]] = []
        heavy = False
        for first, last in windows(start, end, WINDOW_MONTHS):
            payload = await self._call_json(
                "POST",
                PATH_INVOICES,
                {"dateDebutPeriode": first.isoformat(), "dateFinPeriode": last.isoformat()},
            )
            invoices, batch_heavy = parse_invoices(payload)
            batches.append(invoices)
            heavy |= batch_heavy
        return merge_invoices(batches, heavy)

    async def async_get_consumptions(self, start: date, end: date) -> ConsumptionSearch:
        """Billed consumption lines within [start, end], searched in windows of 12 months."""
        batches: list[list[ConsumptionLine]] = []
        partial = False
        for first, last in windows(start, end, WINDOW_MONTHS):
            payload = await self._call_json(
                "POST",
                PATH_CONSUMPTIONS,
                {"dateDebutRecherche": first.isoformat(), "dateFinRecherche": last.isoformat()},
            )
            lines, batch_partial = parse_consumptions(payload)
            batches.append(lines)
            partial |= batch_partial
        return merge_consumptions(batches, partial)

    async def async_download_invoice(self, number: str) -> bytes:
        """The PDF of an invoice (bytes starting with %PDF). Writing it is the caller's job."""
        number = number.strip()
        if not INVOICE_NUMBER_RE.fullmatch(number):
            raise ValueError("invalid invoice number")
        raw = await self._call("GET", PATH_INVOICE_PDF.format(number=number))
        if not raw.startswith(b"%PDF"):
            raise TotalEnergiesTemporaryError("the invoice download is not a PDF")
        return raw
