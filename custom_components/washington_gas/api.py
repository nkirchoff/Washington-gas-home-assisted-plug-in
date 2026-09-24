"""Client for the Washington Gas usage portal, which is hosted by Opower.

Washington Gas publishes meter reads, costs and bill forecasts through an
Opower site at wgl.opower.com (the "Home Energy Analysis" pages). This module
talks to the same JSON API those pages use.

It only depends on aiohttp and the standard library on purpose, so it can
also be run outside Home Assistant (see scripts/check_connection.py).

The request shapes follow the open source opower library
(https://github.com/tronikos/opower, Apache-2.0), which Home Assistant's
built-in Opower integration uses for other utilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Washington Gas serves DC, Maryland and Virginia.
TIMEZONE = ZoneInfo("America/New_York")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

# Opower limits how much history one request may cover.
_MAX_REQUEST_DAYS = {"day": 363, "hour": 26}

# Aggregations each read resolution can serve, coarsest first.
_SUPPORTED_AGGREGATES = {
    "BILLING": ("bill",),
    "DAY": ("bill", "day"),
    "HOUR": ("bill", "day", "hour"),
    "HALF_HOUR": ("bill", "day", "hour"),
    "QUARTER_HOUR": ("bill", "day", "hour"),
    "FIVE_MINUTE": ("bill", "day", "hour"),
}

_FORECAST_QUERY = """
query GetBillForecast {
  billingAccountsConnection(first: 100) {
    edges {
      node {
        billForecast {
          timeInterval
          currentDateTime
          segments {
            serviceAgreement { uuid }
            estimatedUsage { value, unit }
            estimatedUsageCharges { value }
            soFarUsage { value }
            soFarUsageCharges { value }
            priorYearUsage { value }
            priorYearUsageCharges { value }
          }
        }
      }
    }
  }
}
"""


class WashingtonGasError(Exception):
    """Base error for this client."""


class InvalidAuth(WashingtonGasError):
    """The username or password was rejected."""


class CannotConnect(WashingtonGasError):
    """The portal could not be reached or returned an unexpected answer."""


class NoAccounts(WashingtonGasError):
    """The login worked but has no gas accounts linked to it."""


class ApiError(WashingtonGasError):
    """A data request failed."""

    def __init__(self, message: str, url: str, status: int | None = None) -> None:
        """Initialize the error."""
        super().__init__(f"{message} ({url})")
        self.url = url
        self.status = status


class _PortalNotFound(WashingtonGasError):
    """The portal/utility code pair does not exist."""


@dataclass(frozen=True)
class Portal:
    """An Opower site: the opower.com subdomain and the utility code in its API paths."""

    subdomain: str
    utility_code: str

    @property
    def base_url(self) -> str:
        """Return the site root."""
        return f"https://{self.subdomain}.opower.com"


# Washington Gas has two Opower sites. wgl is the one linked from the customer
# pages, so it is tried first. The working one is remembered after setup.
PORTALS: tuple[Portal, ...] = (Portal("wgl", "wgl"), Portal("wglm", "wglm"))


@dataclass(frozen=True)
class Account:
    """A Washington Gas account (one meter) as Opower sees it."""

    customer_uuid: str
    uuid: str
    utility_account_id: str
    # utility_account_id when it is unique for this login, otherwise uuid.
    account_id: str
    meter_type: str
    read_resolution: str

    def supports(self, aggregate: str) -> bool:
        """Return True if reads can be requested at this aggregation."""
        return aggregate in _SUPPORTED_AGGREGATES.get(self.read_resolution, ("bill",))


@dataclass(frozen=True)
class Forecast:
    """The current bill period so far, plus Opower's projection."""

    start_date: date
    end_date: date
    current_date: date
    unit: str
    usage_to_date: float | None
    cost_to_date: float | None
    forecasted_usage: float | None
    forecasted_cost: float | None
    typical_usage: float | None
    typical_cost: float | None


@dataclass(frozen=True)
class CostRead:
    """Usage and cost over one period (an hour, a day or a bill)."""

    start_time: datetime
    end_time: datetime
    consumption: float
    cost: float


def _optional_float(data: Any, key: str = "value") -> float | None:
    """Return data[key] as a float, or None when it is missing or not a number."""
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: str) -> datetime:
    """Parse an Opower timestamp. Values without an offset are local time."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed


def _normalize_unit(unit: Any) -> str:
    """Map Opower's unit names onto THERM or CCF."""
    text = str(unit or "").upper()
    if text in ("TH", "THM", "THERM", "THERMS"):
        return "THERM"
    if text in ("CCF", "HCF"):
        return "CCF"
    return text or "THERM"


class WashingtonGasClient:
    """Read gas usage, cost and forecasts for one Washington Gas login."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        portal: Portal | None = None,
    ) -> None:
        """Initialize.

        The session must have its own cookie jar, since the portal keeps the
        login in a cookie. Pass portal when it is already known.
        """
        self._session = session
        self._username = username
        self._password = password
        self._portal = portal
        self._access_token: str | None = None
        self._customers: list[dict[str, Any]] | None = None

    @property
    def portal(self) -> Portal | None:
        """Return the Opower site in use, once known."""
        return self._portal

    async def async_login(self) -> None:
        """Sign in, finding the right Opower site if it isn't known yet.

        Logins only last a few minutes, so call this before each batch of requests.

        :raises InvalidAuth: the credentials were rejected
        :raises NoAccounts: the login worked but has no accounts
        :raises CannotConnect: the portal could not be reached
        """
        self._customers = None
        self._access_token = None
        if self._portal is not None:
            try:
                await self._async_sign_in(self._portal)
            except _PortalNotFound as err:
                raise CannotConnect(str(err)) from err
            return

        rejected = False
        empty = False
        last_error: Exception | None = None
        for portal in PORTALS:
            try:
                await self._async_sign_in(portal)
            except InvalidAuth as err:
                rejected = True
                last_error = err
                continue
            except (_PortalNotFound, CannotConnect) as err:
                last_error = err
                continue
            # A site can accept the login but hold no accounts, so make sure
            # there is data behind it before settling on it.
            self._portal = portal
            try:
                customers = await self._async_fetch_customers()
            except ApiError as err:
                _LOGGER.debug("Login worked on %s but reading accounts failed: %s", portal.subdomain, err)
                last_error = err
            else:
                if customers:
                    self._customers = customers
                    return
                empty = True
            self._portal = None
            self._access_token = None

        if empty:
            raise NoAccounts("The login worked but no accounts are linked to it")
        if rejected:
            raise InvalidAuth("Washington Gas rejected the username or password")
        raise CannotConnect(f"Could not sign in to Washington Gas: {last_error}")

    async def _async_sign_in(self, portal: Portal) -> None:
        """Post the credentials to one Opower site."""
        sign_in_page = f"{portal.base_url}/ei/x/sign-in-wall?source=intercepted"
        url = f"{portal.base_url}/ei/edge/apis/user-account-control-v1/cws/v1/{portal.utility_code}/account/signin"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            # Load the sign-in page first, the way a browser would, so any
            # cookies it sets are in place for the login request.
            async with self._session.get(sign_in_page, headers=headers, timeout=REQUEST_TIMEOUT):
                pass
            async with self._session.post(
                url,
                json={"username": self._username, "password": self._password},
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Origin": portal.base_url,
                    "Referer": sign_in_page,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                status = resp.status
                body = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(f"Error reaching {portal.base_url}: {err}") from err

        _LOGGER.debug("Sign in on %s returned HTTP %s", portal.subdomain, status)
        # Only an explicit rejection means bad credentials. Throttling and
        # server errors are not something a new password would fix.
        if status in (401, 403):
            raise InvalidAuth(f"Sign in rejected by {portal.subdomain} (HTTP {status})")
        if status == 404:
            raise _PortalNotFound(f"No sign in endpoint on {portal.subdomain} (HTTP 404)")
        if not 200 <= status < 300:
            raise CannotConnect(f"Sign in on {portal.subdomain} failed with HTTP {status}")

        # Most Opower sites keep the login in a cookie and answer with no body.
        # Some also return a token, which then goes in an Authorization header.
        self._access_token = None
        if body.strip():
            try:
                result = json.loads(body)
            except ValueError:
                result = None
            if isinstance(result, dict):
                token = result.get("sessionToken") or result.get("accessToken")
                if isinstance(token, str) and token:
                    self._access_token = token

    def _headers(self, customer_uuid: str | None = None) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if customer_uuid:
            headers["Opower-Selected-Entities"] = json.dumps([f"urn:opower:customer:uuid:{customer_uuid}"])
        return headers

    def _api_url(self, path: str) -> str:
        if self._portal is None:
            raise WashingtonGasError("Not signed in")
        return f"{self._portal.base_url}/ei/edge/apis/{path}"

    async def _async_get(self, url: str, params: dict[str, str], headers: dict[str, str]) -> Any:
        try:
            async with self._session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status >= 400:
                    raise ApiError(f"HTTP {resp.status}", url, resp.status)
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ApiError(f"Request failed: {err}", url) from err

    async def _async_post_graphql(self, query: str, customer_uuid: str) -> Any:
        url = self._api_url("dsm-graphql-v1/cws/graphql")
        try:
            async with self._session.post(
                url,
                json={"query": query},
                headers={**self._headers(customer_uuid), "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status >= 400:
                    raise ApiError(f"HTTP {resp.status}", url, resp.status)
                result = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ApiError(f"Request failed: {err}", url) from err
        if not isinstance(result, dict) or result.get("errors"):
            raise ApiError(f"GraphQL error: {result.get('errors') if isinstance(result, dict) else result}", url)
        return result

    async def _async_fetch_customers(self) -> list[dict[str, Any]]:
        assert self._portal is not None
        url = self._api_url(f"multi-account-v1/cws/{self._portal.utility_code}/customers")
        result = await self._async_get(url, {"offset": "0", "batchSize": "100", "addressFilter": ""}, self._headers())
        if not isinstance(result, dict) or not isinstance(result.get("customers"), list):
            raise ApiError("No customers in response", url)
        return [customer for customer in result["customers"] if isinstance(customer, dict)]

    async def async_get_accounts(self) -> list[Account]:
        """Return every account (meter) on this login."""
        if self._customers is None:
            self._customers = await self._async_fetch_customers()

        raw: list[tuple[str, dict[str, Any]]] = []
        for customer in self._customers:
            for utility_account in customer.get("utilityAccounts") or []:
                if isinstance(utility_account, dict) and utility_account.get("uuid"):
                    raw.append((str(customer.get("uuid", "")), utility_account))

        ids = [str(item.get("preferredUtilityAccountId") or item["uuid"]) for _, item in raw]
        accounts = []
        for (customer_uuid, item), utility_account_id in zip(raw, ids, strict=True):
            read_resolution = str(item.get("readResolution") or "BILLING").upper()
            if read_resolution not in _SUPPORTED_AGGREGATES:
                _LOGGER.debug("Unknown read resolution %s, using bill level reads", read_resolution)
                read_resolution = "BILLING"
            accounts.append(
                Account(
                    customer_uuid=customer_uuid,
                    uuid=str(item["uuid"]),
                    utility_account_id=utility_account_id,
                    account_id=utility_account_id if ids.count(utility_account_id) == 1 else str(item["uuid"]),
                    meter_type=str(item.get("meterType") or "GAS"),
                    read_resolution=read_resolution,
                )
            )
        return accounts

    async def async_get_forecasts(self, accounts: list[Account]) -> dict[str, Forecast]:
        """Return the current bill forecast for each account uuid that has one.

        Forecasts are optional on Opower, so failures are logged and skipped.
        """
        forecasts: dict[str, Forecast] = {}
        known = {account.uuid for account in accounts}
        for customer_uuid in dict.fromkeys(account.customer_uuid for account in accounts):
            try:
                found = await self._async_graphql_forecasts(customer_uuid)
            except ApiError as err:
                _LOGGER.debug("GraphQL forecast unavailable: %s", err)
                found = {}
            if not found:
                try:
                    found = await self._async_rest_forecasts(customer_uuid)
                except ApiError as err:
                    _LOGGER.debug("Forecast unavailable: %s", err)
                    found = {}
            forecasts.update({uuid: forecast for uuid, forecast in found.items() if uuid in known})
        return forecasts

    async def _async_graphql_forecasts(self, customer_uuid: str) -> dict[str, Forecast]:
        result = await self._async_post_graphql(_FORECAST_QUERY, customer_uuid)
        forecasts: dict[str, Forecast] = {}
        edges = ((result.get("data") or {}).get("billingAccountsConnection") or {}).get("edges") or []
        for edge in edges:
            bill_forecast = ((edge or {}).get("node") or {}).get("billForecast")
            if not isinstance(bill_forecast, dict):
                continue
            interval = bill_forecast.get("timeInterval")
            if not isinstance(interval, str) or "/" not in interval:
                continue
            start_text, end_text = interval.split("/", 1)
            try:
                start = _parse_time(start_text).date()
                end = _parse_time(end_text).date()
                current = _parse_time(bill_forecast.get("currentDateTime") or start_text).date()
            except ValueError:
                continue
            for segment in bill_forecast.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                uuid = str((segment.get("serviceAgreement") or {}).get("uuid") or "")
                if not uuid:
                    continue
                forecasts[uuid] = Forecast(
                    start_date=start,
                    end_date=end,
                    current_date=current,
                    unit=_normalize_unit((segment.get("estimatedUsage") or {}).get("unit")),
                    usage_to_date=_optional_float(segment.get("soFarUsage")),
                    cost_to_date=_optional_float(segment.get("soFarUsageCharges")),
                    forecasted_usage=_optional_float(segment.get("estimatedUsage")),
                    forecasted_cost=_optional_float(segment.get("estimatedUsageCharges")),
                    typical_usage=_optional_float(segment.get("priorYearUsage")),
                    typical_cost=_optional_float(segment.get("priorYearUsageCharges")),
                )
        return forecasts

    async def _async_rest_forecasts(self, customer_uuid: str) -> dict[str, Forecast]:
        assert self._portal is not None
        url = self._api_url(
            f"bill-forecast-cws-v1/cws/{self._portal.utility_code}/customers/{customer_uuid}/combined-forecast"
        )
        result = await self._async_get(url, {}, self._headers(customer_uuid))
        forecasts: dict[str, Forecast] = {}
        for item in (result or {}).get("accountForecasts") or []:
            if not isinstance(item, dict) or not item.get("accountUuids"):
                continue
            try:
                start = date.fromisoformat(str(item["startDate"])[:10])
                end = date.fromisoformat(str(item["endDate"])[:10])
                current = date.fromisoformat(str(item["currentDate"])[:10])
            except (KeyError, ValueError):
                continue
            forecasts[str(item["accountUuids"][0])] = Forecast(
                start_date=start,
                end_date=end,
                current_date=current,
                unit=_normalize_unit(item.get("unitOfMeasure")),
                usage_to_date=_optional_float(item, "usageToDate"),
                cost_to_date=_optional_float(item, "costToDate"),
                forecasted_usage=_optional_float(item, "forecastedUsage"),
                forecasted_cost=_optional_float(item, "forecastedCost"),
                typical_usage=_optional_float(item, "typicalUsage"),
                typical_cost=_optional_float(item, "typicalCost"),
            )
        return forecasts

    async def async_get_cost_reads(
        self,
        account: Account,
        aggregate: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[CostRead]:
        """Return usage and cost reads, oldest first.

        aggregate is "bill", "day" or "hour". start and end are local dates and
        both are needed except for bill reads, where leaving them out returns
        the whole bill history.
        """
        if not account.supports(aggregate):
            raise ValueError(f"{aggregate} reads are not available for this account ({account.read_resolution})")
        try:
            reads = await self._async_get_dated_reads(account, aggregate, start, end, usage_only=False)
        except ApiError:
            if aggregate == "bill":
                raise
            # Some sites fail on daily or hourly cost. Usage alone is still useful.
            _LOGGER.debug("Cost reads failed, falling back to usage only reads")
            return await self._async_usage_only(account, aggregate, start, end)

        result = [self._parse_read(read) for read in reads]
        # The newest periods come back as zeros until the data is in.
        while result and result[-1].consumption == 0 and result[-1].cost == 0:
            result.pop()
        if not result and aggregate != "bill":
            # Some sites only price whole bills and return nothing here.
            return await self._async_usage_only(account, aggregate, start, end)
        return result

    async def _async_usage_only(
        self, account: Account, aggregate: str, start: date | None, end: date | None
    ) -> list[CostRead]:
        reads = await self._async_get_dated_reads(account, aggregate, start, end, usage_only=True)
        result = [self._parse_read(read) for read in reads]
        while result and result[-1].consumption == 0:
            result.pop()
        return result

    @staticmethod
    def _parse_read(read: dict[str, Any]) -> CostRead:
        # The cost endpoint puts usage in "value", the usage endpoint in "consumption".
        consumption = read.get("value") if "value" in read else (read.get("consumption") or {}).get("value")
        return CostRead(
            start_time=_parse_time(read["startTime"]),
            end_time=_parse_time(read["endTime"]),
            consumption=float(consumption or 0),
            cost=float(read.get("providedCost") or 0),
        )

    async def _async_get_dated_reads(
        self,
        account: Account,
        aggregate: str,
        start: date | None,
        end: date | None,
        usage_only: bool,
    ) -> list[dict[str, Any]]:
        """Fetch reads, splitting long ranges into the chunks Opower allows."""
        if start is None or end is None:
            if aggregate != "bill":
                raise ValueError("start and end are required unless reading bills")
            return await self._async_fetch_reads(account, aggregate, None, None, usage_only)

        start_dt = datetime.combine(start, time(), TIMEZONE)
        # The end date is inclusive, matching how Opower treats it.
        end_dt = datetime.combine(end, time(), TIMEZONE) + timedelta(days=1)
        max_days = _MAX_REQUEST_DAYS.get(aggregate)

        # Walk backwards from the newest chunk until the start date or until a
        # chunk comes back empty (history only goes back a few years).
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        req_end = end_dt
        while True:
            req_start = start_dt if max_days is None else max(start_dt, req_end - timedelta(days=max_days))
            if req_start > req_end:
                return result
            reads = await self._async_fetch_reads(account, aggregate, req_start, req_end, usage_only)
            if not reads:
                return result
            # Chunks can overlap by one read around daylight saving changes.
            fresh = [read for read in reads if read["startTime"] not in seen]
            seen.update(read["startTime"] for read in fresh)
            result = fresh + result
            req_end = req_start - timedelta(days=1)

    async def _async_fetch_reads(
        self,
        account: Account,
        aggregate: str,
        start: datetime | None,
        end: datetime | None,
        usage_only: bool,
    ) -> list[dict[str, Any]]:
        assert self._portal is not None
        params = {"aggregateType": aggregate}
        if usage_only:
            url = self._api_url(
                f"DataBrowser-v1/cws/utilities/{self._portal.utility_code}/utilityAccounts/{account.uuid}/reads"
            )
            if start is not None:
                params["startDate"] = start.date().isoformat()
            if end is not None:
                params["endDate"] = end.date().isoformat()
        else:
            url = self._api_url(f"DataBrowser-v1/cws/cost/utilityAccount/{account.uuid}")
            if start is not None:
                params["startDate"] = start.isoformat()
            if end is not None:
                params["endDate"] = end.isoformat()
        try:
            result = await self._async_get(url, params, self._headers(account.customer_uuid))
        except ApiError as err:
            # Bill requests fail with a server error when the range starts
            # before the account existed. That just means there is nothing.
            if err.status == 500 and aggregate == "bill":
                _LOGGER.debug("Ignoring server error for bill reads: %s", err)
                return []
            raise
        reads = result.get("reads") if isinstance(result, dict) else None
        if not isinstance(reads, list):
            raise ApiError("No reads in response", url)
        return [read for read in reads if isinstance(read, dict) and "startTime" in read and "endTime" in read]
