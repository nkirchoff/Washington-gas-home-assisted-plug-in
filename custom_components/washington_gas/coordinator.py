"""Fetch Washington Gas data and feed it to sensors and the Energy dashboard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import Any

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .api import (
    TIMEZONE,
    Account,
    ApiError,
    CannotConnect,
    CostRead,
    Forecast,
    InvalidAuth,
    NoAccounts,
    Portal,
    WashingtonGasClient,
)
from .const import (
    CONF_ENERGY_UNIT,
    CONF_PORTAL_SUBDOMAIN,
    CONF_PORTAL_UTILITY_CODE,
    DEFAULT_ENERGY_UNIT,
    DOMAIN,
    ENERGY_UNIT_KWH,
    KWH_PER_THERM,
)

_LOGGER = logging.getLogger(__name__)

type WashingtonGasConfigEntry = ConfigEntry[WashingtonGasCoordinator]

# How far back the first import reaches at each resolution.
_FIRST_IMPORT_DAYS = {"day": 3 * 365, "hour": 60}
# Later updates re-read this much recent history so there is always overlap
# with what is already stored.
_OVERLAP_DAYS = 30


@dataclass(frozen=True)
class AccountData:
    """Everything the sensors show for one account."""

    account: Account
    # THERM or CCF, as Washington Gas reports it.
    unit: str
    forecast: Forecast | None
    latest_day: CostRead | None
    latest_bill: CostRead | None


def create_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Create a session with its own cookie jar, which holds the portal login."""
    # Some Opower sites reject quoted cookies.
    return async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar(quote_cookie=False))


def portal_from_data(data: Mapping[str, Any]) -> Portal | None:
    """Return the remembered Opower site from config entry data."""
    subdomain = data.get(CONF_PORTAL_SUBDOMAIN)
    utility_code = data.get(CONF_PORTAL_UTILITY_CODE)
    if subdomain and utility_code:
        return Portal(subdomain, utility_code)
    return None


def _merge_reads(coarse: list[CostRead], fine: list[CostRead]) -> list[CostRead]:
    """Combine reads so every moment is counted once, preferring the finer ones.

    Coarse reads (say, bills) are kept only for the time before the fine reads
    (say, days) begin. A coarse read that straddles that point is kept whole,
    and fine reads inside it are dropped, so nothing is counted twice.
    """
    if not fine:
        return coarse
    fine_start = fine[0].start_time
    kept: list[CostRead] = []
    boundary = fine_start
    for read in coarse:
        if read.end_time <= fine_start:
            kept.append(read)
        elif read.start_time < fine_start:
            kept.append(read)
            boundary = max(boundary, read.end_time)
    return kept + [read for read in fine if read.start_time >= boundary]


class WashingtonGasCoordinator(DataUpdateCoordinator[dict[str, AccountData]]):
    """Log in, read accounts, update sensors and import usage history."""

    config_entry: WashingtonGasConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: WashingtonGasConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            # Opower updates once a day, usually a day or two behind.
            update_interval=timedelta(hours=12),
        )
        self.client = WashingtonGasClient(
            create_session(hass),
            config_entry.data[CONF_USERNAME],
            config_entry.data[CONF_PASSWORD],
            portal_from_data(config_entry.data),
        )
        self.energy_unit: str = config_entry.data.get(CONF_ENERGY_UNIT, DEFAULT_ENERGY_UNIT)
        # Remember each account's unit so sensors keep it if a forecast goes missing.
        self._units: dict[str, str] = {}

        @callback
        def _keep_polling() -> None:
            """Keep updates running even if every sensor is disabled.

            The Energy dashboard history is imported during updates, and the
            coordinator stops polling when nothing listens to it.
            """

        config_entry.async_on_unload(self.async_add_listener(_keep_polling))

    async def _async_update_data(self) -> dict[str, AccountData]:
        """Fetch fresh data. Logins expire within minutes, so log in each time."""
        try:
            await self.client.async_login()
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (CannotConnect, NoAccounts) as err:
            raise UpdateFailed(f"Could not log in to Washington Gas: {err}") from err
        self._async_remember_portal()

        try:
            accounts = await self.client.async_get_accounts()
        except ApiError as err:
            raise UpdateFailed(f"Could not read Washington Gas accounts: {err}") from err
        if not accounts:
            raise UpdateFailed("No accounts were found on this Washington Gas login")

        forecasts = await self.client.async_get_forecasts(accounts)
        data: dict[str, AccountData] = {}
        for account in accounts:
            forecast = forecasts.get(account.uuid)
            if forecast is not None:
                self._units[account.uuid] = forecast.unit
            # Washington Gas bills in therms, so assume that until told otherwise.
            unit = self._units.get(account.uuid, "THERM")
            data[account.account_id] = AccountData(
                account=account,
                unit=unit,
                forecast=forecast,
                latest_day=await self._async_latest_read(account, "day", 14) if account.supports("day") else None,
                latest_bill=await self._async_latest_read(account, "bill", 120),
            )
            try:
                await self._async_insert_statistics(account, unit)
            except ApiError as err:
                _LOGGER.warning(
                    "Could not update Energy dashboard history for account %s, will retry next update: %s",
                    account.utility_account_id,
                    err,
                )
        return data

    @callback
    def _async_remember_portal(self) -> None:
        """Save which Opower site worked so later logins go straight to it."""
        portal = self.client.portal
        if portal is None or portal == portal_from_data(self.config_entry.data):
            return
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_PORTAL_SUBDOMAIN: portal.subdomain,
                CONF_PORTAL_UTILITY_CODE: portal.utility_code,
            },
        )

    async def _async_latest_read(self, account: Account, aggregate: str, days: int) -> CostRead | None:
        """Return the newest read at this aggregation, or None."""
        today = datetime.now(TIMEZONE).date()
        try:
            reads = await self.client.async_get_cost_reads(account, aggregate, today - timedelta(days=days), today)
        except ApiError as err:
            _LOGGER.debug("Could not read latest %s usage: %s", aggregate, err)
            return None
        return reads[-1] if reads else None

    def statistic_ids(self, account: Account, unit: str) -> tuple[str, str]:
        """Return the (usage, cost) statistic ids for an account."""
        base = f"{DOMAIN}:{slugify(account.account_id)}"
        usage_id = f"{base}_gas_usage_kwh" if self._uses_kwh(unit) else f"{base}_gas_usage"
        return usage_id, f"{base}_gas_cost"

    def _uses_kwh(self, unit: str) -> bool:
        # Only therms convert exactly to kWh. CCF is a volume, and its energy
        # content varies, so CCF data stays in CCF.
        return self.energy_unit == ENERGY_UNIT_KWH and unit == "THERM"

    async def _async_insert_statistics(self, account: Account, unit: str) -> None:
        """Add new usage and cost history to the long-term statistics."""
        usage_id, cost_id = self.statistic_ids(account, unit)
        if self._uses_kwh(unit):
            factor = KWH_PER_THERM
            usage_unit: str = UnitOfEnergy.KILO_WATT_HOUR
            usage_unit_class = EnergyConverter.UNIT_CLASS
        else:
            factor = 1.0
            usage_unit = UnitOfVolume.CENTUM_CUBIC_FEET
            usage_unit_class = VolumeConverter.UNIT_CLASS

        name = f"Washington Gas {account.utility_account_id}"
        usage_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{name} gas usage",
            source=DOMAIN,
            statistic_id=usage_id,
            unit_class=usage_unit_class,
            unit_of_measurement=usage_unit,
        )
        cost_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{name} gas cost",
            source=DOMAIN,
            statistic_id=cost_id,
            unit_class=None,
            unit_of_measurement=None,
        )

        recorder = get_instance(self.hass)
        last_stat = await recorder.async_add_executor_job(get_last_statistics, self.hass, 1, usage_id, True, {"sum"})

        if not last_stat:
            _LOGGER.debug("Importing full history for %s", usage_id)
            reads = await self._async_get_reads_for_statistics(account, None)
            usage_sum = 0.0
            cost_sum = 0.0
            last_time: float | None = None
        else:
            since = datetime.fromtimestamp(last_stat[usage_id][0]["start"], TIMEZONE).date()
            reads = await self._async_get_reads_for_statistics(account, since - timedelta(days=_OVERLAP_DAYS))
            if not reads:
                _LOGGER.debug("No recent reads for %s", usage_id)
                return
            # Find the running totals just before the first read we got back.
            start = reads[0].start_time
            stats: dict[str, list[Any]] = {}
            for end in (start + timedelta(seconds=1), None):
                stats = await recorder.async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start,
                    end,
                    {usage_id, cost_id},
                    "hour",
                    None,
                    {"sum"},
                )
                if stats.get(usage_id):
                    break
            if not stats.get(usage_id):
                # Everything returned is newer than what is stored. The
                # totals continue from the last stored value.
                stats = {usage_id: last_stat[usage_id]}
                cost_last = await recorder.async_add_executor_job(
                    get_last_statistics, self.hass, 1, cost_id, True, {"sum"}
                )
                if cost_last:
                    stats[cost_id] = cost_last[cost_id]
            usage_sum = _sum_of(stats.get(usage_id))
            cost_sum = _sum_of(stats.get(cost_id))
            last_time = float(stats[usage_id][0]["start"])

        usage_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []
        for read in reads:
            # Statistics are hourly, so a period is filed under the hour it starts in.
            start = read.start_time.replace(minute=0, second=0, microsecond=0)
            if last_time is not None and start.timestamp() <= last_time:
                continue
            usage = max(0.0, read.consumption) * factor
            cost = max(0.0, read.cost)
            usage_sum += usage
            cost_sum += cost
            usage_stats.append(StatisticData(start=start, state=usage, sum=usage_sum))
            cost_stats.append(StatisticData(start=start, state=cost, sum=cost_sum))
            last_time = start.timestamp()

        _LOGGER.debug("Adding %s statistics to %s and %s", len(usage_stats), usage_id, cost_id)
        if usage_stats:
            async_add_external_statistics(self.hass, usage_metadata, usage_stats)
            async_add_external_statistics(self.hass, cost_metadata, cost_stats)

    async def _async_get_reads_for_statistics(self, account: Account, since: date | None) -> list[CostRead]:
        """Return reads at the finest resolution available for each stretch of time.

        With since=None this reads all bills, the last 3 years of days and the
        last 60 days of hours (where the meter supports them).
        """
        today = datetime.now(TIMEZONE).date()
        reads = await self.client.async_get_cost_reads(account, "bill", since, today if since else None)
        for aggregate in ("day", "hour"):
            if not account.supports(aggregate):
                break
            oldest = today - timedelta(days=_FIRST_IMPORT_DAYS[aggregate])
            if since is None:
                start = oldest
            else:
                # Start no later than the first coarse read, so a bill that is
                # already stored can't hide newer days that aren't yet.
                first = reads[0].start_time.astimezone(TIMEZONE).date() if reads else since
                start = max(min(since, first), oldest)
            finer = await self.client.async_get_cost_reads(account, aggregate, start, today)
            reads = _merge_reads(reads, finer)
        return sorted(reads, key=lambda read: read.start_time)


def _sum_of(records: list[Any] | None) -> float:
    if records and records[0].get("sum") is not None:
        return float(records[0]["sum"])
    return 0.0
