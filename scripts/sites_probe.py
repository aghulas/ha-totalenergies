#!/usr/bin/env python3
"""Supervised, one-shot probe: why does the config flow see no delivery point?

Run by hand, by the account owner, in a terminal:

    .venv/bin/python scripts/sites_probe.py

Same safety as live_check.py (one `POST /login` at most, sentinel file, DataDome stops,
tokens not stored). Calls: login, GET sites, GET accounts, GET sites again. The report holds
only shapes — key names, counts, value TYPES, energy codes — never a number, name or address.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import sys
from datetime import UTC, datetime
from typing import Any

import aiohttp
from live_check import ROOT, SENTINEL, keychain_password

from custom_components.totalenergies_pro.api import (
    TotalEnergiesAuthError,
    TotalEnergiesBlockedError,
    TotalEnergiesClient,
    TotalEnergiesError,
)
from custom_components.totalenergies_pro.api.const import PATH_SITES
from custom_components.totalenergies_pro.api.models import parse_sites

REPORT = ROOT / "captures" / "sites-probe.json"


def shape(payload: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": type(payload).__name__}
    if isinstance(payload, list):
        out["length"] = len(payload)
        return out
    if not isinstance(payload, dict):
        return out
    out["keys"] = sorted(payload)
    rows = payload.get("sites")
    out["sites_type"] = type(rows).__name__
    if isinstance(rows, list):
        out["rows"] = []
        for row in rows:
            r: dict[str, Any] = {"row_keys": sorted(row) if isinstance(row, dict) else None}
            inner = row.get("site") if isinstance(row, dict) else None
            if isinstance(inner, dict):
                r["site_keys"] = sorted(inner)
                r["pod_type"] = type(inner.get("pod")).__name__
                r["energie"] = (
                    inner.get("energie")
                    if inner.get("energie") in ("01", "02")
                    else (type(inner.get("energie")).__name__)
                )
            r_parsed = parse_sites({"sites": [row]})
            r["parsed_point_set"] = bool(r_parsed and r_parsed[0].point)
            r["parsed_energy"] = r_parsed[0].energy if r_parsed else None
            out["rows"].append(r)
    return out


async def run(email: str, password: str, report: dict[str, Any]) -> None:
    async with aiohttp.ClientSession() as http:
        client = TotalEnergiesClient(http, email, password)
        SENTINEL.write_text("password sent, outcome unknown\n")
        await client.async_login()
        SENTINEL.unlink()
        report["login"] = "ok"
        report["sites_first"] = shape(await client._call_json("GET", PATH_SITES))
        report["accounts_count"] = len(await client.async_get_accounts())
        report["sites_after_accounts"] = shape(await client._call_json("GET", PATH_SITES))


def main() -> int:
    if SENTINEL.exists():
        print(f"{SENTINEL.name} exists: a previous run sent the password without success.")
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
