"""Diagnostics, redacted (CLAUDE.md rule 6): no password, token, e-mail, account, contract,
delivery point or invoice number, no name or address.

Points are numbered ("point_1"…); their label (possibly a site name) is not included.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_POINTS
from .coordinator import TotalEnergiesConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TotalEnergiesConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    state = coordinator.state
    labels = {p: f"point_{n}" for n, p in enumerate(entry.data[CONF_POINTS], start=1)}

    def iso(moment: Any) -> str | None:
        return moment.isoformat() if moment else None

    points: dict[str, Any] = {}
    data = coordinator.data
    for point, summary in data.points.items() if data else []:
        if point not in labels:
            continue
        info = summary.as_dict()
        if info.get("last_invoice"):
            info["last_invoice"] = {k: v for k, v in info["last_invoice"].items() if k != "number"}
        points[labels[point]] = {
            "energy": entry.data[CONF_POINTS][point].get("energy"),
            "classes": state.classes.get(point, []),
            "first_day": iso(state.first_day.get(point)),
            **info,
        }
    auth_error = coordinator.client.auth_error
    return {
        "entry": {"version": entry.version, "points": len(labels), "options": dict(entry.options)},
        "state": {
            "auth_refused": state.auth_refused,
            "auth_reason": state.auth_reason,
            "session_stored": state.tokens is not None,
            "last_attempt": iso(state.last_attempt),
            "last_success": iso(state.last_success),
            "last_result": state.last_result,
            "last_error": state.last_error,
            "last_error_at": iso(state.last_error_at),
            "blocked_until": iso(state.blocked_until),
            "blocked_count": state.blocked_count,
            "backfilled_years": state.backfilled_years,
        },
        "client": {
            "auth_error": auth_error.reason if auth_error else None,
            "auth_status": auth_error.status if auth_error else None,
            "blocked_until": coordinator.client.blocked_until,
        },
        "points": points,
    }
