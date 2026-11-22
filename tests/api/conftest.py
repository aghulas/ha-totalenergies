"""A local fake of api-b2b.totalenergies.fr. Everything here is synthetic."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.totalenergies_pro.api import Tokens, TotalEnergiesClient

USERNAME = "compte@exemple.invalid"
PASSWORD = 'synth "pass\\word" é'  # quotes and a backslash on purpose
NOW = 1_800_000_000.0  # fixed wall clock of the tests (epoch seconds)

PRM = "00000000000001"
PCE = "GI000001"
INVOICE_1 = "100000000001"
INVOICE_2 = "100000000002"

PDF = b"%PDF-1.4 synthetic\n%%EOF"

DATADOME_HEADERS = {"X-DataDome": "protected", "X-DataDome-CID": "synthetic-cid"}


def jwt(exp: float) -> str:
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': int(exp), 'sub': 'synthetic'})}.sig"


def invoice(number: str, day: str, energy: str = "01", ttc: str = "120,50") -> dict[str, Any]:
    return {
        "facture": {
            "numero": number,
            "date": day,
            "secteurActivite": energy,
            "type": "FACTURE",
            "nbPOD": "1",
            "consoTotalKWH": "1000",
            "montantSousTotalHT": "80,00",
            "sommeTaxes": "20,42",
            "montantTotalHorsTVA": "100,42",
            "montantTVA": "20,08",
            "montantTTC": ttc,
            "dateImpression": day,
            "numeroFactureAnnulation": "",
            "dateImpressionAnnulation": "0000-00-00",
        }
    }


def line(number: str, start: str, end: str, cls: str, kwh: str, ht: str) -> dict[str, Any]:
    return {
        "pod": PRM,
        "energie": "01",
        "dateDebutPeriode": start,
        "dateFinPeriode": end,
        "classeTemporelle": cls,
        "rang": "1",
        "calFou": "CAL1",
        "libelleCalFou": "Calendrier synthétique",
        "quantiteConsommationKWH": kwh,
        "montantHT": ht,
        "numeroFacture": number,
        "dateFacture": end,
    }


class FakeTotalEnergies:
    """Mutable behaviour of the fake API, and the record of what it received."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, Any]] = []
        self.auth_headers: list[str | None] = []
        self.accepted_password = PASSWORD
        self.login_mode = "normal"  # | "refused_401" | "server_error" | "datadome"
        self.refresh_mode = "normal"  # | "refused" | "server_error"
        self.api_mode = "normal"  # | "datadome" | "server_error" | "html"
        self.token_lifetime = 3600.0
        self.expires_style = "seconds"  # | "missing" | "ms"
        self.valid_tokens: set[str] = set()
        self.reject_next = 0  # answer 401 to the next n data calls
        self.counter = 0
        self.invoices: list[dict[str, Any]] = []
        self.consumptions: list[dict[str, Any]] = []
        self.heavy_load = False
        self.sites_null = False
        self.partial = False
        self.pdf = PDF

    def count(self, path: str) -> int:
        return sum(1 for _, p in self.calls if p == path)

    @property
    def data_calls(self) -> list[str]:
        return [p for _, p in self.calls if p.startswith("/web-methods/")]

    def _session(self) -> dict[str, Any]:
        self.counter += 1
        token = jwt(NOW + self.token_lifetime) + str(self.counter)
        self.valid_tokens.add(token)
        out: dict[str, Any] = {
            "token": token,
            "oid": "synthetic-oid",
            "refreshToken": f"refresh-{self.counter}",
        }
        if self.expires_style == "seconds":
            out["expiresAt"] = int(NOW + self.token_lifetime)
        elif self.expires_style == "ms":
            out["expiresAt"] = int((NOW + self.token_lifetime) * 1000)
        return out

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.calls.append((request.method, request.path))
        body = await request.json() if request.can_read_body else None
        self.bodies.append((request.path, body))
        self.auth_headers.append(request.headers.get("Authorization"))
        if request.path == "/login":
            return self._login(body)
        if request.path == "/refresh-token":
            return self._refresh(body)
        return self._data(request)

    def _login(self, body: Any) -> web.StreamResponse:
        if self.login_mode == "datadome":
            return web.json_response({"url": "x"}, status=403, headers=DATADOME_HEADERS)
        if self.login_mode == "server_error":
            return web.json_response({"message": "boom"}, status=500)
        if body != {"username": USERNAME, "password": self.accepted_password}:
            if self.login_mode == "refused_401":
                return web.json_response({"error": "invalid"}, status=401)
            return web.json_response([])  # the real refusal: HTTP 200 with []
        return web.json_response(self._session())

    def _refresh(self, body: Any) -> web.StreamResponse:
        if self.refresh_mode == "server_error":
            return web.json_response({"message": "boom"}, status=503)
        if self.refresh_mode == "refused" or not str(body.get("refreshToken", "")).startswith(
            "refresh-"
        ):
            return web.json_response([])
        return web.json_response(self._session())

    def _data(self, request: web.Request) -> web.StreamResponse:
        if self.api_mode == "datadome":
            return web.json_response(
                {"url": "https://geo.captcha-delivery.com/captcha/"},
                status=403,
                headers=DATADOME_HEADERS,
            )
        if self.api_mode == "server_error":
            return web.json_response({"message": "boom"}, status=500)
        auth = request.headers.get("Authorization", "")
        if self.reject_next > 0 or auth.removeprefix("Bearer ") not in self.valid_tokens:
            self.reject_next = max(0, self.reject_next - 1)
            return web.json_response({"message": "Unauthorized"}, status=401)
        if self.api_mode == "html":
            return web.Response(text="<html>maintenance</html>", content_type="text/html")
        path = request.path
        if path == "/web-methods/comptes":
            return web.json_response(
                {"comptes": [{"compte": {"numero": "200000000001", "nom": "CLIENT SYNTH"}}]}
            )
        if path == "/web-methods/sites":
            if self.sites_null:
                return web.json_response({"sites": None})
            return web.json_response({"sites": [{"site": {"pod": PRM, "energie": "01"}}]})
        if path == "/web-methods/factures":
            return web.json_response({"factures": self.invoices, "isHeavyLoad": self.heavy_load})
        if path == "/web-methods/consommations":
            return web.json_response(
                {"consumptions": self.consumptions, "details": {"hasError": self.partial}}
            )
        if path.startswith("/web-methods/download/facture/"):
            return web.Response(body=self.pdf, content_type="application/pdf")
        return web.json_response({"message": "not found"}, status=404)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def fake(socket_enabled: None) -> AsyncIterator[tuple[FakeTotalEnergies, str]]:
    # socket_enabled: pytest-homeassistant-custom-component blocks sockets; the fake is local.
    state = FakeTotalEnergies()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", state.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield state, str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


@pytest.fixture
async def http(socket_enabled: None) -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_client(
    fake: tuple[FakeTotalEnergies, str],
    http: aiohttp.ClientSession,
    clock: Clock,
    *,
    password: str | None = PASSWORD,
    tokens: Tokens | None = None,
    **kwargs: Any,
) -> TotalEnergiesClient:
    options: dict[str, Any] = {"min_interval": 0, "clock": clock}
    options.update(kwargs)
    return TotalEnergiesClient(http, USERNAME, password, tokens=tokens, base_url=fake[1], **options)
