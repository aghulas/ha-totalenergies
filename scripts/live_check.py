#!/usr/bin/env python3
"""Supervised, one-shot check of the API client against the real account.

Run by hand, by the account owner, in a terminal:

    .venv/bin/python scripts/live_check.py

Safety (CLAUDE.md rules 1, 2, 6):
- the password comes from the macOS Keychain item of the MCP connector (service
  `pro.totalenergies.fr`, account = the e-mail typed below) or, failing that, from a prompt —
  never an argument, an env var or a file; it is sent AT MOST ONCE (one `POST /login`);
- a sentinel file is written before the password leaves and removed only on success: after a
  refusal (or an unknown outcome) the script refuses to run again until the owner deletes it;
- a DataDome block stops the script at once;
- the tokens are not stored; the PDF is downloaded to memory and only its size is printed;
- the report holds counts, energies, time classes and statuses — no number, name, address or
  amount. It is printed and written to captures/live-check.json (git-ignored).
"""

from __future__ import annotations

import asyncio
import getpass
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.totalenergies_pro.api import (  # noqa: E402
    TotalEnergiesAuthError,
    TotalEnergiesBlockedError,
    TotalEnergiesClient,
    TotalEnergiesError,
)

SENTINEL = ROOT / ".probe-refused"
REPORT = ROOT / "captures" / "live-check.json"
KEYCHAIN_SERVICE = "pro.totalenergies.fr"


def keychain_password(email: str) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                email,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.rstrip("\n")
    return out if r.returncode == 0 and out else None


async def run(email: str, password: str, report: dict[str, Any]) -> None:
    today = date.today()
    async with aiohttp.ClientSession() as http:
        client = TotalEnergiesClient(http, email, password)

        SENTINEL.write_text("password sent, outcome unknown\n")
        await client.async_login()
        SENTINEL.unlink()
        assert client.tokens is not None
        report["login"] = {
            "ok": True,
            "refresh_token": client.tokens.refresh_token is not None,
            "token_lifetime_min": round(
                (client.tokens.expires_at - datetime.now(UTC).timestamp()) / 60
            ),
        }

        accounts = await client.async_get_accounts()
        report["accounts"] = {
            "count": len(accounts),
            "segments": sorted({a.segment or "?" for a in accounts}),
        }

        sites = await client.async_get_sites()
        report["sites"] = [
            {
                "energy": s.energy,
                "segment": s.segment,
                "status": s.contract.status,
                "status_code": s.contract.status_code,
                "has_start": s.contract.start is not None,
                "termination_set": s.contract.termination is not None,
                "power_classes": sorted(s.contract.subscribed_power_kva),
                "has_meter": s.meter is not None,
            }
            for s in sites
        ]

        # Checks the route without the trailing "?" (aiohttp cannot send it; see const.py).
        invoices = await client.async_get_invoices(today - timedelta(days=365), today)
        report["invoices_12_months"] = {
            "count": len(invoices.invoices),
            "per_energy": dict(Counter(i.energy or "?" for i in invoices.invoices)),
            "heavy_load": invoices.heavy_load,
            "cancelled": sum(i.cancelled for i in invoices.invoices),
            "all_dated": all(i.date is not None for i in invoices.invoices),
            "all_ttc": all(i.total_incl_vat is not None for i in invoices.invoices),
        }

        lines = await client.async_get_consumptions(today - timedelta(days=365), today)
        report["consumptions_12_months"] = {
            "count": len(lines.lines),
            "partial": lines.partial,
            "per_energy": dict(Counter(x.energy or "?" for x in lines.lines)),
            "time_classes": sorted({x.time_class or "-" for x in lines.lines}),
            "all_periods": all(x.start and x.end for x in lines.lines),
            "all_amounts": all(x.amount_excl_tax is not None for x in lines.lines),
        }

        latest = next((i.number for i in invoices.invoices if i.number), None)
        if latest:
            pdf = await client.async_download_invoice(latest)
            report["pdf"] = {"bytes": len(pdf), "is_pdf": pdf.startswith(b"%PDF")}

        report["http_calls_hint"] = (
            "1 login + 1 accounts + 1 sites + 1-2 invoices + 1-2 lines + 1 pdf"
        )


def main() -> int:
    if SENTINEL.exists():
        print(f"{SENTINEL.name} exists: a previous run sent the password without success.")
        print("Check the account on pro.totalenergies.fr, then delete the file by hand.")
        return 2
    email = input("TotalEnergies e-mail: ").strip()
    password = keychain_password(email) or getpass.getpass("Password (not echoed): ")
    report: dict[str, Any] = {"started": datetime.now(UTC).isoformat(timespec="seconds")}
    code = 0
    try:
        asyncio.run(run(email, password, report))
    except TotalEnergiesAuthError as err:
        report["error"] = f"auth: {err.reason} (HTTP {err.status})"
        code = 1
    except TotalEnergiesBlockedError:
        report["error"] = "DataDome block: stop for today"
        code = 1
    except TotalEnergiesError as err:
        report["error"] = f"{type(err).__name__}: {err}"
        code = 1
    finally:
        del password
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(text + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
