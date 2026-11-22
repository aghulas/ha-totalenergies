"""Constants of the totalenergies_pro integration."""

from __future__ import annotations

from typing import Final
from zoneinfo import ZoneInfo

DOMAIN: Final = "totalenergies_pro"

# Config entry data
CONF_POINTS: Final = "points"  # {point: {"energy": …, "label": …}}

# Options
CONF_HISTORY_YEARS: Final = "history_years"
CONF_FETCH_HOUR: Final = "fetch_hour"
CONF_SPREAD: Final = "spread"
CONF_PDF_FOLDER: Final = "pdf_folder"
CONF_READ_PDFS: Final = "read_pdfs"

DEFAULT_HISTORY_YEARS: Final = 3
DEFAULT_FETCH_HOUR: Final = 11  # invoices are issued around the 10th
DEFAULT_PDF_FOLDER: Final = ""  # empty: PDF download disabled
DEFAULT_READ_PDFS: Final = True
PDF_PER_FETCH: Final = 6  # new invoice PDFs read per daily fetch (the first year takes a few days)
PDF_DAYS: Final = 365  # PDFs of the invoices issued over this many days are read and kept
PDF_MAX_FAILURES: Final = 3  # a PDF that cannot be read is skipped after this many attempts
# Version of the PDF reading: a kept result read by another version is read again (2: layout of
# November 2026, certificates per kWhc).
PDF_READER: Final = 2
SUGGESTED_PDF_FOLDER: Final = "/media/totalenergies"

# Billing periods are French local dates, whatever the HA time zone.
PARIS: Final = ZoneInfo("Europe/Paris")

MAX_JITTER_SECONDS: Final = 30 * 60  # random delay added to the daily fetch hour
REIMPORT_DAYS: Final = 365  # searched at each daily fetch (one call per search)
EDGE_DAYS: Final = 92  # oldest days of a partial search, not re-imported (late invoices)
MANUAL_REFRESH_COOLDOWN_SECONDS: Final = 300
MIN_ATTEMPT_GAP_HOURS: Final = 6  # no automatic fetch at startup sooner after an attempt
INTERRUPTED_RETRY_MINUTES: Final = 15  # …unless that attempt was cut short (HA restarted)
BLOCKED_ISSUE_AFTER: Final = 2  # DataDome blocks in a row before a Repairs issue

ISSUE_AUTH_REFUSED: Final = "auth_refused"
ISSUE_BLOCKED: Final = "datadome_blocked"

SERVICE_DOWNLOAD_INVOICE: Final = "download_invoice"
ATTR_INVOICE: Final = "invoice"
ATTR_CONFIG_ENTRY: Final = "config_entry_id"
