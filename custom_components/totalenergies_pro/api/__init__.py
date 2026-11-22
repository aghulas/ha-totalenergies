"""Read-only access to the TotalEnergies business customer API (no Home Assistant dependency)."""

from .client import Tokens, TotalEnergiesClient, check_call, is_datadome, public_path
from .const import API_BASE
from .errors import (
    TotalEnergiesAuthError,
    TotalEnergiesBlockedError,
    TotalEnergiesError,
    TotalEnergiesRouteNotAllowed,
    TotalEnergiesTemporaryError,
)
from .models import (
    Account,
    ConsumptionLine,
    ConsumptionSearch,
    Contract,
    Energy,
    Invoice,
    InvoiceSearch,
    Site,
    parse_accounts,
    parse_consumptions,
    parse_invoices,
    parse_sites,
    sites_from_lines,
    windows,
)

__all__ = [
    "API_BASE",
    "Account",
    "ConsumptionLine",
    "ConsumptionSearch",
    "Contract",
    "Energy",
    "Invoice",
    "InvoiceSearch",
    "Site",
    "Tokens",
    "TotalEnergiesAuthError",
    "TotalEnergiesBlockedError",
    "TotalEnergiesClient",
    "TotalEnergiesError",
    "TotalEnergiesRouteNotAllowed",
    "TotalEnergiesTemporaryError",
    "check_call",
    "is_datadome",
    "parse_accounts",
    "parse_consumptions",
    "parse_invoices",
    "parse_sites",
    "public_path",
    "sites_from_lines",
    "windows",
]
