"""Import of the billed series into Home Assistant long-term statistics.

Each day is one hourly row at 00:00 Europe/Paris. Imports are idempotent: the days of the
re-imported window are overwritten (a day that no longer has a value is set to 0), and the
cumulative sums of every later row are recomputed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance

from .const import DOMAIN, PARIS

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

KIND_BILLED_ENERGY = "billed_energy"
KIND_ENERGY_COST = "energy_cost"
KIND_COST_EXCL_VAT = "cost_excl_vat"
KIND_COST_INCL_VAT = "cost_incl_vat"


def statistic_id(point: str, kind: str) -> str:
    return f"{DOMAIN}:{point.lower()}_{kind}"


def day_start(day: date) -> datetime:
    """00:00 Europe/Paris on `day`, in UTC — always on the hour, as statistics require."""
    return datetime.combine(day, time(0), PARIS).astimezone(UTC)


async def async_first_row(hass: HomeAssistant, stat_id: str) -> date | None:
    """The day of the oldest row of a statistic, if any."""
    rows: dict[str, list[Any]] = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, _EPOCH, None, {stat_id}, "hour", None, {"state"}
    )
    series = rows.get(stat_id) or []
    if not series:
        return None
    return datetime.fromtimestamp(series[0]["start"], UTC).astimezone(PARIS).date()


async def async_import_daily(
    hass: HomeAssistant,
    stat_id: str,
    name: str,
    unit: str,
    unit_class: str | None,
    values: dict[date, float],
    window_start: date | None,
) -> int:
    """Import daily amounts as a cumulative statistic. Rows from `window_start` on are replaced
    by `values` (absent days → 0); older rows are kept. Returns the number of rows written."""
    first_new = min(values) if values else None
    first_day = (
        min(d for d in (window_start, first_new) if d is not None)
        if (window_start or first_new)
        else None
    )
    if first_day is None:
        return 0
    first = day_start(first_day).timestamp()
    new = {day_start(day).timestamp(): round(value, 6) for day, value in values.items()}
    existing: dict[str, list[Any]] = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, _EPOCH, None, {stat_id}, "hour", None, {"state", "sum"}
    )
    base = 0.0
    merged: dict[float, float] = {}
    for row in existing.get(stat_id, []):
        if row["start"] < first:
            base = row.get("sum") or 0.0
        elif row.get("state") is not None:
            merged[row["start"]] = 0.0 if window_start is not None else row["state"]
    merged.update(new)
    if not merged:
        return 0

    rows: list[StatisticData] = []
    total = base
    for start in sorted(merged):
        total += merged[start]
        rows.append(
            StatisticData(
                start=datetime.fromtimestamp(start, UTC), state=merged[start], sum=round(total, 6)
            )
        )
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=stat_id,
            unit_class=unit_class,
            unit_of_measurement=unit,
        ),
        rows,
    )
    return len(rows)
