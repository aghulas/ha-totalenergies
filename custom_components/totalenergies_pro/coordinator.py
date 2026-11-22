"""Daily fetch of the TotalEnergies data, import into statistics, summaries for the entities.

Polling policy (CLAUDE.md rules 1 to 3):
- one fetch per day, at the configured hour plus a random delay; at startup only if no fetch
  succeeded today and no attempt was made in the last hours. Three searches per fetch (sites,
  invoices, consumption lines over the last 12 months), more only when backfilling;
- a temporary failure is not retried: the next attempt is the next scheduled fetch;
- a DataDome block is persisted: nothing is sent until it ends (next day), even across restarts;
  a Repairs issue appears when it happens twice in a row;
- an authentication refusal is persisted, stops the schedule and starts the reauth flow. Until
  the user re-authenticates — even across restarts — no request is sent to TotalEnergies.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_EURO, UnitOfEnergy
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Site,
    Tokens,
    TotalEnergiesAuthError,
    TotalEnergiesBlockedError,
    TotalEnergiesClient,
    TotalEnergiesTemporaryError,
)
from .billing import SPREAD_PER_DAY, PointSeries, PointSummary, build_series, summarize
from .const import (
    BLOCKED_ISSUE_AFTER,
    CONF_FETCH_HOUR,
    CONF_HISTORY_YEARS,
    CONF_POINTS,
    CONF_SPREAD,
    DEFAULT_FETCH_HOUR,
    DEFAULT_HISTORY_YEARS,
    DOMAIN,
    EDGE_DAYS,
    INTERRUPTED_RETRY_MINUTES,
    ISSUE_AUTH_REFUSED,
    ISSUE_BLOCKED,
    MANUAL_REFRESH_COOLDOWN_SECONDS,
    MAX_JITTER_SECONDS,
    MIN_ATTEMPT_GAP_HOURS,
    PARIS,
    REIMPORT_DAYS,
)
from .statistics import (
    KIND_BILLED_ENERGY,
    KIND_COST_EXCL_VAT,
    KIND_COST_INCL_VAT,
    KIND_ENERGY_COST,
    async_first_row,
    async_import_daily,
    statistic_id,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
_LONG_NUMBER = re.compile(r"\d{6,}")

type TotalEnergiesConfigEntry = ConfigEntry[TotalEnergiesCoordinator]


def storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}"


def make_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, storage_key(entry_id), private=True)


def _moment(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) and value else None


@dataclass(slots=True)
class FetchState:
    """What must survive a restart."""

    auth_refused: bool = False
    auth_reason: str | None = None
    tokens: dict[str, Any] | None = None  # the TotalEnergies session (no password)
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    # "running" while a fetch is in progress: still "running" at startup means it was cut short.
    last_result: str | None = None
    last_error: str | None = None  # no number, no credentials
    last_error_at: datetime | None = None
    blocked_until: datetime | None = None
    blocked_count: int = 0
    backfilled_years: int = 0  # history depth covered by the last complete import
    spread: str | None = None  # spreading method of that import
    first_day: dict[str, date] = field(default_factory=dict)  # per point: oldest day imported
    classes: dict[str, list[str]] = field(default_factory=dict)  # per point: time classes seen
    summaries: dict[str, dict[str, Any]] = field(default_factory=dict)  # per point

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FetchState:
        data = data or {}
        return cls(
            auth_refused=bool(data.get("auth_refused")),
            auth_reason=data.get("auth_reason"),
            tokens=data.get("tokens") if isinstance(data.get("tokens"), dict) else None,
            last_attempt=_moment(data.get("last_attempt")),
            last_success=_moment(data.get("last_success")),
            last_result=data.get("last_result"),
            last_error=data.get("last_error"),
            last_error_at=_moment(data.get("last_error_at")),
            blocked_until=_moment(data.get("blocked_until")),
            blocked_count=int(data.get("blocked_count") or 0),
            backfilled_years=int(data.get("backfilled_years") or 0),
            spread=data.get("spread"),
            first_day={p: date.fromisoformat(d) for p, d in (data.get("first_day") or {}).items()},
            classes={p: list(c) for p, c in (data.get("classes") or {}).items()},
            summaries=dict(data.get("summaries") or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        def iso(moment: datetime | None) -> str | None:
            return moment.isoformat() if moment else None

        return {
            "auth_refused": self.auth_refused,
            "auth_reason": self.auth_reason,
            "tokens": self.tokens,
            "last_attempt": iso(self.last_attempt),
            "last_success": iso(self.last_success),
            "last_result": self.last_result,
            "last_error": self.last_error,
            "last_error_at": iso(self.last_error_at),
            "blocked_until": iso(self.blocked_until),
            "blocked_count": self.blocked_count,
            "backfilled_years": self.backfilled_years,
            "spread": self.spread,
            "first_day": {p: d.isoformat() for p, d in self.first_day.items()},
            "classes": self.classes,
            "summaries": self.summaries,
        }


async def async_clear_auth_refused(
    hass: HomeAssistant, entry_id: str, tokens: Tokens | None
) -> None:
    """Called by the reauth flow once new credentials have been accepted by TotalEnergies."""
    store = make_store(hass, entry_id)
    state = FetchState.from_dict(await store.async_load())
    state.auth_refused, state.auth_reason = False, None
    state.tokens = tokens.as_dict() if tokens else None
    await store.async_save(state.as_dict())
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_AUTH_REFUSED}_{entry_id}")


@callback
def async_create_auth_issue(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_AUTH_REFUSED}_{entry.entry_id}",
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_AUTH_REFUSED,
        translation_placeholders={"title": entry.title},
    )


@dataclass(frozen=True, slots=True)
class TotalEnergiesData:
    fetched_at: datetime
    points: dict[str, PointSummary]


class TotalEnergiesCoordinator(DataUpdateCoordinator[TotalEnergiesData]):
    """One TotalEnergies account: its delivery points, its schedule, its persisted state."""

    config_entry: TotalEnergiesConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TotalEnergiesConfigEntry,
        client: TotalEnergiesClient,
        store: Store[dict[str, Any]],
        state: FetchState,
    ) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=None)
        self.client = client
        self.state = state
        self._store = store
        self._unsub: CALLBACK_TYPE | None = None
        # Show the last known values right after a restart, without asking TotalEnergies again.
        restored = {
            p: PointSummary.from_dict(s) for p, s in state.summaries.items() if p in self.points
        }
        if restored and state.last_success:
            self.data = TotalEnergiesData(fetched_at=state.last_success, points=restored)

    # ---- configuration ---------------------------------------------------------------------

    def _option(self, key: str, default: Any) -> Any:
        return self.config_entry.options.get(key, default)

    @property
    def points(self) -> dict[str, dict[str, Any]]:
        """Selected delivery points: {point: {"energy": …, "label": …}}."""
        return dict(self.config_entry.data[CONF_POINTS])

    def point_label(self, point: str) -> str:
        info = self.points.get(point) or {}
        return str(info.get("label") or info.get("energy") or "TotalEnergies")

    @property
    def _history_years(self) -> int:
        return int(self._option(CONF_HISTORY_YEARS, DEFAULT_HISTORY_YEARS))

    @property
    def _fetch_hour(self) -> int:
        return int(self._option(CONF_FETCH_HOUR, DEFAULT_FETCH_HOUR))

    @property
    def _spread(self) -> str:
        return str(self._option(CONF_SPREAD, SPREAD_PER_DAY))

    @callback
    def on_tokens(self, tokens: Tokens) -> None:
        """New session from the client: kept in the state, saved at the end of the fetch."""
        self.state.tokens = tokens.as_dict()

    # ---- schedule --------------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        self.config_entry.async_on_unload(self._async_cancel)
        self._async_schedule_next()
        if self._startup_fetch_needed():
            self.config_entry.async_create_background_task(
                self.hass, self.async_refresh(), f"{DOMAIN} startup fetch"
            )

    def _blocked(self, now: datetime) -> bool:
        return self.state.blocked_until is not None and now < self.state.blocked_until

    def _startup_fetch_needed(self) -> bool:
        now = dt_util.now()
        if self._blocked(now):
            return False
        gap = (
            timedelta(minutes=INTERRUPTED_RETRY_MINUTES)
            if self.state.last_result == "running"
            else timedelta(hours=MIN_ATTEMPT_GAP_HOURS)
        )
        if self.state.last_attempt and now - self.state.last_attempt < gap:
            return False
        if self._history_years > self.state.backfilled_years or self._spread != self.state.spread:
            return True
        last = self.state.last_success
        return last is None or last.astimezone(PARIS).date() < now.astimezone(PARIS).date()

    @callback
    def _async_schedule_next(self) -> None:
        self._async_cancel()
        now = dt_util.now()
        target = now.replace(hour=self._fetch_hour, minute=0, second=0, microsecond=0)
        target += timedelta(seconds=random.uniform(0, MAX_JITTER_SECONDS))
        if target <= now:
            target += timedelta(days=1)
        self._unsub = async_track_point_in_time(self.hass, self._async_scheduled, target)

    @callback
    def _async_cancel(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_scheduled(self, _now: datetime) -> None:
        self._unsub = None
        await self.async_refresh()
        if not self.state.auth_refused:
            self._async_schedule_next()

    async def async_manual_refresh(self) -> None:
        """Refresh asked by the user (button): never after a refusal or during a block, and
        rate-limited."""
        if self.state.auth_refused:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="reauth_needed")
        now = dt_util.now()
        if self._blocked(now):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="blocked")
        last = self.state.last_attempt
        if last and now - last < timedelta(seconds=MANUAL_REFRESH_COOLDOWN_SECONDS):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="refresh_too_soon")
        await self.async_refresh()

    # ---- fetch -----------------------------------------------------------------------------

    async def _async_save(self) -> None:
        await self._store.async_save(self.state.as_dict())

    def _record_error(self, message: str) -> None:
        """Keep the reason of the last failure for the diagnostics, without any number."""
        self.state.last_error = _LONG_NUMBER.sub("<n>", message)[:300]
        self.state.last_error_at = dt_util.now()

    async def _async_update_data(self) -> TotalEnergiesData:
        if self.state.auth_refused:
            raise ConfigEntryAuthFailed("TotalEnergies refused the credentials; reauth needed")
        if self._blocked(dt_util.now()):
            raise UpdateFailed("blocked by the TotalEnergies anti-bot protection until tomorrow")
        self.state.last_attempt = dt_util.now()
        self.state.last_result = "running"
        await self._async_save()
        try:
            data = await self._async_fetch()
        except TotalEnergiesAuthError as err:
            self.state.auth_refused, self.state.auth_reason = True, err.reason
            self.state.tokens = None
            self.state.last_result = "auth_refused"
            self._record_error(str(err))
            self._async_cancel()
            async_create_auth_issue(self.hass, self.config_entry)
            raise ConfigEntryAuthFailed(str(err)) from err
        except TotalEnergiesBlockedError as err:
            self.state.blocked_until = datetime.fromtimestamp(err.retry_after, UTC)
            self.state.blocked_count += 1
            self.state.last_result = "blocked"
            self._record_error(str(err))
            if self.state.blocked_count >= BLOCKED_ISSUE_AFTER:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"{ISSUE_BLOCKED}_{self.config_entry.entry_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_BLOCKED,
                    translation_placeholders={"title": self.config_entry.title},
                )
            raise UpdateFailed(str(err)) from err
        except TotalEnergiesTemporaryError as err:
            self.state.last_result = "failed"
            self._record_error(str(err))
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            self.state.last_result = "failed"
            self._record_error(f"unexpected {type(err).__name__}: {err}")
            raise
        finally:
            await self._async_save()
        self.state.last_success = data.fetched_at
        self.state.last_result = "ok"
        self.state.blocked_until, self.state.blocked_count = None, 0
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_BLOCKED}_{self.config_entry.entry_id}")
        self.state.summaries = {p: s.as_dict() for p, s in data.points.items()}
        await self._async_save()
        return data

    async def _history_lost(self) -> bool:
        """The oldest imported day is no longer in the statistics (recorder database reset)."""
        for point, first in self.state.first_day.items():
            if point not in self.points:
                continue
            found = await async_first_row(self.hass, statistic_id(point, KIND_BILLED_ENERGY))
            if found is None or found > first:
                _LOGGER.warning("TotalEnergies history missing from the statistics: re-importing")
                return True
        return False

    async def _async_fetch(self) -> TotalEnergiesData:
        today = dt_util.now(PARIS).date()
        depth = self._history_years
        method = self._spread
        backfill = (
            depth > self.state.backfilled_years
            or method != self.state.spread
            or await self._history_lost()
        )
        start = today - timedelta(days=365 * depth if backfill else REIMPORT_DAYS)

        try:
            sites: list[Site] | None = await self.client.async_get_sites()
        except TotalEnergiesBlockedError:
            raise
        except TotalEnergiesTemporaryError as err:
            # The site list alone is missing (seen as `{"sites": null}`): the billing data is
            # still worth importing; the contracts keep their last known values.
            _LOGGER.warning("TotalEnergies site list unavailable (%s); contracts kept", err)
            sites = None
        invoices = (await self.client.async_get_invoices(start, today)).invoices
        search = await self.client.async_get_consumptions(start, today)
        lines = search.lines
        if search.partial:
            _LOGGER.debug("TotalEnergies flagged the consumption search as partial (usual)")

        point_energy = {p: str(info.get("energy")) for p, info in self.points.items()}
        # Backfill: everything from the oldest day is rewritten. Daily fetch: the oldest days of
        # the search are left alone (lines or invoices outside the search may contribute).
        since = None if backfill else start + timedelta(days=EDGE_DAYS)
        series = build_series(invoices, lines, point_energy, method, since)
        for point, s in series.items():
            await self._async_import(point, s, since, backfill)

        if backfill:
            self.state.backfilled_years, self.state.spread = depth, method
        summaries = summarize(invoices, lines, sites or [], point_energy, today)
        if sites is None and self.data is not None:
            for point, summary in summaries.items():
                previous = self.data.points.get(point)
                if previous is not None and previous.contract is not None:
                    summaries[point] = replace(summary, contract=previous.contract)
        _LOGGER.debug(
            "TotalEnergies fetch: %d sites, %d invoices, %d lines (backfill: %s)",
            len(sites) if sites is not None else -1,
            len(invoices),
            len(lines),
            backfill,
        )
        return TotalEnergiesData(fetched_at=dt_util.now(), points=summaries)

    async def _async_import(
        self, point: str, s: PointSeries, since: date | None, backfill: bool
    ) -> None:
        label = f"TotalEnergies {self.point_label(point)}"
        days = [d for values in (s.billed_energy, s.cost_incl_vat) for d in values]
        window = since if since is not None else (min(days) if days else None)
        kwh, eur = UnitOfEnergy.KILO_WATT_HOUR, CURRENCY_EURO
        await async_import_daily(
            self.hass,
            statistic_id(point, KIND_BILLED_ENERGY),
            f"{label} billed energy",
            kwh,
            "energy",
            s.billed_energy,
            window,
        )
        classes = sorted(set(self.state.classes.get(point, [])) | set(s.billed_energy_by_class))
        for cls in classes:
            await async_import_daily(
                self.hass,
                statistic_id(point, f"{KIND_BILLED_ENERGY}_{cls}"),
                f"{label} billed energy {cls.upper()}",
                kwh,
                "energy",
                s.billed_energy_by_class.get(cls, {}),
                window,
            )
        self.state.classes[point] = classes
        for kind, name, values in (
            (KIND_ENERGY_COST, "energy cost (excl. tax)", s.energy_cost),
            (KIND_COST_EXCL_VAT, "cost (excl. VAT)", s.cost_excl_vat),
            (KIND_COST_INCL_VAT, "cost (incl. VAT)", s.cost_incl_vat),
        ):
            await async_import_daily(
                self.hass, statistic_id(point, kind), f"{label} {name}", eur, None, values, window
            )
        if backfill and s.billed_energy:
            self.state.first_day[point] = min(s.billed_energy)
