"""A small in-memory stand-in for the Washington Gas Opower site.

It answers the same URLs the client calls, with response shapes taken from
the opower library, so tests exercise the real client code end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import json
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")

USERNAME = "user@example.com"
PASSWORD = "correct-horse"
CUSTOMER_UUID = "c0ffee00-0000-4000-8000-000000000001"
ACCOUNT_UUID = "acc00000-0000-4000-8000-000000000002"
ACCOUNT_NUMBER = "1234567890"
# A session token some Opower sites return from sign-in instead of a cookie.
SESSION_TOKEN = "opower-session-token-5d7e9b3c1a2f"


def midnight(day: date) -> datetime:
    """Return local midnight at the start of day."""
    return datetime.combine(day, time(), TZ)


def daily_therms(day: date) -> float:
    """Deterministic therms for a day, whole numbers so sums are exact."""
    return float(1 + day.toordinal() % 7)


@dataclass
class Read:
    """One read the fake site serves."""

    start: datetime
    end: datetime
    therms: float
    cost: float

    def as_json(self, usage_only: bool) -> dict[str, Any]:
        base = {"startTime": self.start.isoformat(), "endTime": self.end.isoformat()}
        if usage_only:
            return {**base, "consumption": {"value": self.therms, "type": "ACTUAL"}}
        return {**base, "value": self.therms, "providedCost": self.cost, "readType": "ACTUAL"}


def build_daily(first: date, last: date) -> list[Read]:
    """Daily reads for every day from first to last, inclusive."""
    reads = []
    day = first
    while day <= last:
        therms = daily_therms(day)
        reads.append(Read(midnight(day), midnight(day + timedelta(days=1)), therms, therms * 1.25))
        day += timedelta(days=1)
    return reads


def build_bills(first_start: date, last_end: date) -> list[Read]:
    """Bills running from the 5th of one month to the 5th of the next."""
    reads = []
    start = first_start
    while True:
        end = (start.replace(day=1) + timedelta(days=32)).replace(day=5)
        if end > last_end:
            return reads
        therms = sum(daily_therms(start + timedelta(days=n)) for n in range((end - start).days))
        reads.append(Read(midnight(start), midnight(end), therms, therms * 1.25))
        start = end


class FakeResponse:
    """Enough of aiohttp.ClientResponse for the client."""

    def __init__(self, status: int, body: Any = None) -> None:
        self.status = status
        self._text = body if isinstance(body, str) else ("" if body is None else json.dumps(body))

    async def text(self, errors: str = "strict") -> str:
        return self._text

    async def json(self, content_type: str | None = None) -> Any:
        return json.loads(self._text)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeCookieJar:
    """Clearing cookies ends every session on the fake site, like a real browser."""

    def __init__(self, site: FakeOpower) -> None:
        self._site = site

    def clear(self) -> None:
        self._site.signed_in.clear()


@dataclass
class FakeOpower:
    """The fake Opower sites (wgl.opower.com and wglm.opower.com). Change the fields to shape each test."""

    subdomains: set[str] = field(default_factory=lambda: {"wgl"})
    read_resolution: str = "DAY"
    forecast_mode: str | None = "graphql"  # "graphql", "rest" or None
    daily: list[Read] = field(default_factory=list)
    bills: list[Read] = field(default_factory=list)
    login_status: int | None = None  # force a sign in status
    # Answer sign in with SESSION_TOKEN in the body instead of setting a cookie.
    token_login: bool = False
    cost_endpoint_fails: bool = False
    signed_in: set[str] = field(default_factory=set)
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    # Hosts the password was sent to.
    password_sent_to: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cookie_jar = FakeCookieJar(self)

    @classmethod
    def with_history(cls, **kwargs: Any) -> FakeOpower:
        """A site with 400 days of daily reads and about 40 months of bills."""
        today = datetime.now(TZ).date()
        last = today - timedelta(days=2)
        site = cls(**kwargs)
        site.daily = build_daily(today - timedelta(days=400), last)
        site.bills = build_bills(date(today.year - 4, 1, 5), last)
        return site

    # aiohttp.ClientSession interface -----------------------------------

    def get(self, url: Any, params: dict[str, str] | None = None, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, params=params, **kwargs)

    def post(self, url: Any, json: Any = None, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, json=json, **kwargs)

    def request(
        self,
        method: str,
        url: Any,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> FakeResponse:
        url = str(url)
        params = dict(params or {})
        self.calls.append((method.upper(), url, params))
        if isinstance(json, dict) and PASSWORD in json.values():
            self.password_sent_to.append(urlsplit(url).hostname or "")
        return self._route(method.upper(), url, params, json, headers or {})

    # Opower --------------------------------------------------------------

    def _route(
        self,
        method: str,
        url: str,
        params: dict[str, str],
        body: Any,
        headers: dict[str, str],
    ) -> FakeResponse:
        parts = urlsplit(url)
        subdomain = parts.hostname.split(".")[0] if parts.hostname else ""
        path = parts.path
        if subdomain not in self.subdomains:
            return FakeResponse(404, "not found")
        if path == "/ei/x/sign-in-wall":
            return FakeResponse(
                200, '<html><form><input name="username"/><input type="password" name="password"/></form></html>'
            )
        if path == f"/ei/edge/apis/user-account-control-v1/cws/v1/{subdomain}/account/signin" and method == "POST":
            if self.login_status is not None:
                return FakeResponse(self.login_status, "")
            if body != {"username": USERNAME, "password": PASSWORD}:
                return FakeResponse(401, "")
            if self.token_login:
                return FakeResponse(200, {"sessionToken": SESSION_TOKEN})
            self.signed_in.add(subdomain)
            return FakeResponse(204)
        signed_in = subdomain in self.signed_in
        if self.token_login:
            signed_in = headers.get("Authorization") == f"Bearer {SESSION_TOKEN}"
        if not signed_in:
            return FakeResponse(401, "")
        if path == f"/ei/edge/apis/multi-account-v1/cws/{subdomain}/customers":
            return FakeResponse(200, self._customers())
        if path == "/ei/edge/apis/dsm-graphql-v1/cws/graphql":
            if self.forecast_mode != "graphql":
                return FakeResponse(200, {"errors": [{"message": "not available"}]})
            return FakeResponse(200, self._graphql_forecast())
        if path == f"/ei/edge/apis/bill-forecast-cws-v1/cws/{subdomain}/customers/{CUSTOMER_UUID}/combined-forecast":
            if self.forecast_mode != "rest":
                return FakeResponse(500, "")
            return FakeResponse(200, self._rest_forecast())
        if path == f"/ei/edge/apis/DataBrowser-v1/cws/cost/utilityAccount/{ACCOUNT_UUID}":
            self._check_selected(headers)
            if self.cost_endpoint_fails and params["aggregateType"] != "bill":
                return FakeResponse(500, "")
            return FakeResponse(200, {"reads": self._reads(params, usage_only=False)})
        if path == f"/ei/edge/apis/DataBrowser-v1/cws/utilities/{subdomain}/utilityAccounts/{ACCOUNT_UUID}/reads":
            self._check_selected(headers)
            return FakeResponse(200, {"reads": self._reads(params, usage_only=True)})
        return FakeResponse(404, "not found")

    @staticmethod
    def _check_selected(headers: dict[str, str]) -> None:
        selected = json.loads(headers["Opower-Selected-Entities"])
        assert selected == [f"urn:opower:customer:uuid:{CUSTOMER_UUID}"]

    def _customers(self) -> dict[str, Any]:
        return {
            "customers": [
                {
                    "uuid": CUSTOMER_UUID,
                    "utilityAccounts": [
                        {
                            "uuid": ACCOUNT_UUID,
                            "preferredUtilityAccountId": ACCOUNT_NUMBER,
                            "meterType": "GAS",
                            "readResolution": self.read_resolution,
                        }
                    ],
                }
            ]
        }

    def _reads(self, params: dict[str, str], usage_only: bool) -> list[dict[str, Any]]:
        aggregate = params["aggregateType"]
        if aggregate == "day" and self.read_resolution == "BILLING":
            raise AssertionError("client asked for daily reads on a bill-only account")
        source = self.bills if aggregate == "bill" else self.daily
        if "startDate" in params:
            start = self._parse(params["startDate"])
            # Opower includes the whole end date.
            end = self._parse(params["endDate"]) + timedelta(days=1)
            if aggregate == "bill":
                source = [read for read in source if read.end > start and read.start < end]
            else:
                source = [read for read in source if start <= read.start < end]
        return [read.as_json(usage_only) for read in source]

    @staticmethod
    def _parse(value: str) -> datetime:
        if "T" in value:
            return datetime.fromisoformat(value)
        return midnight(date.fromisoformat(value))

    def _graphql_forecast(self) -> dict[str, Any]:
        return {
            "data": {
                "billingAccountsConnection": {
                    "edges": [
                        {
                            "node": {
                                "billForecast": {
                                    "timeInterval": "2026-01-05T00:00:00-05:00/2026-02-04T00:00:00-05:00",
                                    "currentDateTime": "2026-01-20T00:00:00-05:00",
                                    "segments": [
                                        {
                                            "serviceAgreement": {"uuid": ACCOUNT_UUID},
                                            "estimatedUsage": {"value": 95.0, "unit": "THERM"},
                                            "estimatedUsageCharges": {"value": 180.5},
                                            "soFarUsage": {"value": 48.0},
                                            "soFarUsageCharges": {"value": 91.2},
                                            "priorYearUsage": {"value": 102.0},
                                            "priorYearUsageCharges": {"value": 170.0},
                                        }
                                    ],
                                }
                            }
                        }
                    ]
                }
            }
        }

    def _rest_forecast(self) -> dict[str, Any]:
        return {
            "totalMetadata": [],
            "accountForecasts": [
                {
                    "preferredUtilityAccountId": ACCOUNT_NUMBER,
                    "accountUuids": [ACCOUNT_UUID],
                    "meterType": "GAS",
                    "startDate": "2026-01-05",
                    "endDate": "2026-02-04",
                    "currentDate": "2026-01-20",
                    "unitOfMeasure": "THERM",
                    "usageToDate": 40,
                    "costToDate": 80,
                    "forecastedUsage": 90,
                    "forecastedCost": 170,
                    "typicalUsage": 100,
                    "typicalCost": 160,
                }
            ],
        }

    def expected_history(self) -> list[Read]:
        """The reads a full import should store: bills until the daily data starts, then days."""
        if not self.daily or self.read_resolution == "BILLING":
            return list(self.bills)
        daily_start = self.daily[0].start
        kept = []
        boundary = daily_start
        for bill in self.bills:
            if bill.end <= daily_start:
                kept.append(bill)
            elif bill.start < daily_start:
                kept.append(bill)
                boundary = max(boundary, bill.end)
        return kept + [read for read in self.daily if read.start >= boundary]
