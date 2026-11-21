"""Constants of the TotalEnergies business customer API (see docs/api.md)."""

from __future__ import annotations

import re
from typing import Final

API_BASE: Final = "https://api-b2b.totalenergies.fr"

PATH_LOGIN: Final = "/login"
PATH_REFRESH: Final = "/refresh-token"
PATH_ACCOUNTS: Final = "/web-methods/comptes"
PATH_SITES: Final = "/web-methods/sites"
# The web front and the MCP connector (httpx) call "/web-methods/factures?". aiohttp (yarl) cannot
# send an empty query and drops the "?"; HTTP-wise both are the same request target. To confirm
# on the live validation (docs/api.md).
PATH_INVOICES: Final = "/web-methods/factures"
PATH_CONSUMPTIONS: Final = "/web-methods/consommations"
PATH_INVOICE_PDF: Final = "/web-methods/download/facture/{number}"

INVOICE_NUMBER_PATTERN: Final = r"\d{6,20}"
INVOICE_NUMBER_RE: Final = re.compile(INVOICE_NUMBER_PATTERN)

# Read-only allow-list (CLAUDE.md rule 5): the client refuses any other call.
ALLOWED: Final = (
    ("POST", re.compile(re.escape(PATH_LOGIN))),
    ("POST", re.compile(re.escape(PATH_REFRESH))),
    ("GET", re.compile(re.escape(PATH_ACCOUNTS))),
    ("GET", re.compile(re.escape(PATH_SITES))),
    ("POST", re.compile(re.escape(PATH_INVOICES))),
    ("POST", re.compile(re.escape(PATH_CONSUMPTIONS))),
    ("GET", re.compile(rf"/web-methods/download/facture/{INVOICE_NUMBER_PATTERN}")),
)

# Identifies the integration honestly instead of posing as a browser.
USER_AGENT: Final = (
    "Mozilla/5.0 (compatible; ha-totalenergies/0.1; +https://github.com/aghulas/ha-totalenergies)"
)
HEADERS: Final = {"Accept": "application/json, text/plain, */*", "User-Agent": USER_AGENT}

MIN_INTERVAL: Final = 1.0  # seconds between two HTTP calls (CLAUDE.md rule 3)
REQUEST_TIMEOUT: Final = 60.0
TOKEN_MARGIN: Final = 60.0  # a token expiring within this many seconds is renewed first
DEFAULT_TOKEN_LIFETIME: Final = 50 * 60  # when neither expiresAt nor the JWT exp is readable
MIN_LOGIN_INTERVAL: Final = 3600.0  # never send the password twice within this many seconds
DATADOME_BACKOFF: Final = 20 * 3600.0  # after an anti-bot block, no call for this long
WINDOW_MONTHS: Final = 12  # the POST searches accept at most 12 months
