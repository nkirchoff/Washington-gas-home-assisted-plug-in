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
# Secrets the hand-off passes along. Tests check none of them leak into logs or traces.
SAML_ASSERTION = "PHNhbWxwOlJlc3BvbnNlIHNpZ25lZD0idHJ1ZSIvPg=="
SSO_CODE = "one-time-sso-code-8c1f2a"
EMBEDDED_TOKEN = "opower-embedded-token-5d7e9b3c1a2f"


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

    def __init__(self, status: int, body: Any = None, location: str | None = None) -> None:
        self.status = status
        self._text = body if isinstance(body, str) else ("" if body is None else json.dumps(body))
        self.headers = {"Location": location} if location else {}

    async def text(self, errors: str = "strict") -> str:
        return self._text

    async def json(self, content_type: str | None = None) -> Any:
        return json.loads(self._text)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeCookieJar:
    """Clearing cookies ends every session on the fake sites, like a real browser."""

    def __init__(self, site: FakeOpower) -> None:
        self._site = site

    def clear(self) -> None:
        self._site.signed_in.clear()
        self._site.portal_session = False


def _redirect(location: str) -> FakeResponse:
    return FakeResponse(302, "", location)


@dataclass
class FakeOpower:
    """The fake sites (my.washingtongas.com and *.opower.com). Change the fields to shape each test."""

    subdomains: set[str] = field(default_factory=lambda: {"wgl"})
    read_resolution: str = "DAY"
    forecast_mode: str | None = "graphql"  # "graphql", "rest" or None
    daily: list[Read] = field(default_factory=list)
    bills: list[Read] = field(default_factory=list)
    login_status: int | None = None  # force a direct Opower sign in status
    cost_endpoint_fails: bool = False
    # My Washington Gas. None means my.washingtongas.com can't be reached.
    # Otherwise how it hands off to Opower: see _portal().
    portal_mode: str | None = None
    # Whether wgl.opower.com's own sign-in accepts USERNAME/PASSWORD.
    direct_login_works: bool = True
    signed_in: set[str] = field(default_factory=set)
    portal_session: bool = False
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
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
        **kwargs: Any,
    ) -> FakeResponse:
        url = str(url)
        params = dict(params or {})
        self.calls.append((method.upper(), url, params))
        host = urlsplit(url).hostname or ""
        sent = dict(data or {}) | (json if isinstance(json, dict) else {})
        if PASSWORD in sent.values():
            self.password_sent_to.append(host)
        # The client follows redirects itself so signed URLs keep their encoding.
        assert allow_redirects is False or host.endswith("opower.com"), "portal requests must not auto-redirect"
        if host == "my.washingtongas.com":
            return self._portal(method.upper(), url, data or {})
        return self._route(method.upper(), url, params, json, data or {}, headers or {})

    # My Washington Gas ---------------------------------------------------

    _LOGIN_PAGE = """<html><head><title>My Washington Gas</title>{extra_head}</head><body>
<form method="post" action="./default.aspx" id="form1">
<input type="hidden" name="__VIEWSTATE" value="vs-login" />
<input type="hidden" name="__EVENTVALIDATION" value="ev-login" />
<input type="text" name="ctl00$Main$txtSearch" value="" />
{fields}
<span class="error" style="display:none">Invalid username or password.</span>
{message}
</form>
<a href="/portal/ForgotPassword.aspx">Forgot username or password?</a>
</body></html>"""

    _LOGIN_FIELDS = """<input type="email" name="ctl00$Main$txtUserName" />
<input type="password" name="ctl00$Main$txtPassword" />
<input type="checkbox" name="ctl00$Main$chkRemember" />
<input type="submit" name="ctl00$Main$btnLogin" value="Sign In" />
<input type="submit" name="ctl00$Main$btnRegister" value="Register" />"""

    _POSTBACK_LOGIN_FIELDS = """<input type="text" name="ctl00$Main$txtUserName" />
<input type="password" name="ctl00$Main$txtPassword" />
<input type="hidden" name="__EVENTTARGET" value="" />
<input type="hidden" name="__EVENTARGUMENT" value="" />
</form><form><a href="javascript:__doPostBack('ctl00$Main$lnkLogin','')">Sign In</a>"""

    def _login_page(self, message: str = "") -> FakeResponse:
        mode = self.portal_mode
        if mode == "js_only":
            return FakeResponse(
                200,
                '<html><body><div id="app"></div><script src="/portal/js/login.bundle.js"></script></body></html>',
            )
        extra_head = '<script src="https://www.google.com/recaptcha/api.js"></script>' if mode == "captcha" else ""
        fields = self._POSTBACK_LOGIN_FIELDS if mode == "postback" else self._LOGIN_FIELDS
        return FakeResponse(200, self._LOGIN_PAGE.format(extra_head=extra_head, fields=fields, message=message))

    def _portal(self, method: str, url: str, data: dict[str, str]) -> FakeResponse:
        mode = self.portal_mode
        if mode is None:
            return FakeResponse(503, "Service Unavailable")
        path = urlsplit(url).path
        query = urlsplit(url).query
        if path == "/" and method == "GET":
            return _redirect("/portal/default.aspx")
        if path == "/portal/default.aspx" and method == "GET":
            return self._login_page()
        if path == "/portal/default.aspx" and method == "POST":
            assert data.get("__VIEWSTATE") == "vs-login", "the form's hidden fields must be sent back"
            assert "ctl00$Main$btnRegister" not in data, "only the first submit button is pressed"
            assert "ctl00$Main$chkRemember" not in data, "unchecked boxes aren't sent"
            if mode == "postback":
                assert data.get("__EVENTTARGET") == "ctl00$Main$lnkLogin"
            username = data.get("ctl00$Main$txtUserName")
            password = data.get("ctl00$Main$txtPassword")
            if mode == "ignores_form":
                return self._login_page()
            if (username, password) != (USERNAME, PASSWORD):
                return self._login_page('<div class="alert">The email or password you entered is incorrect.</div>')
            if mode == "mfa":
                return FakeResponse(
                    200,
                    '<html><body><form method="post" action="Verify.aspx">'
                    "<p>Enter the verification code we sent to your phone.</p>"
                    '<input type="text" name="ctl00$Main$txtVerificationCode" />'
                    '<input type="submit" name="btnVerify" value="Verify" /></form></body></html>',
                )
            self.portal_session = True
            return _redirect("/portal/Dashboard.aspx")
        if not self.portal_session:
            return _redirect("/portal/default.aspx")
        if path == "/portal/Logout.aspx":
            self.portal_session = False
            return _redirect("/portal/default.aspx")
        if path == "/portal/Dashboard.aspx":
            return self._dashboard(method, data)
        if path == "/portal/Usage.aspx":
            if mode == "token":
                return FakeResponse(
                    200,
                    '<html><head><script src="https://wgl.opower.com/ei/x/embedded-api/loader.js"></script>'
                    '<script>var mapConfig = { accessToken: "pk.mapbox-decoy-token-000000000000" };</script>'
                    f'<script>var opowerConfig = {{ utility: "wgl", accessToken: "{EMBEDDED_TOKEN}" }};</script>'
                    '</head><body><div id="opower-widget"></div></body></html>',
                )
            return FakeResponse(
                200,
                '<html><body><h1>Usage</h1><a href="/portal/Dashboard.aspx">Home</a>'
                '<a href="/portal/OpowerSSO.aspx?target=hea&amp;x=1">Home Energy Analysis</a></body></html>',
            )
        if path == "/portal/OpowerSSO.aspx":
            assert query == "target=hea&x=1"
            return FakeResponse(
                200,
                '<html><body onload="document.forms[0].submit()">'
                '<form method="post" action="https://wgl.opower.com/ei/sso/saml/acs">'
                f'<input type="hidden" name="SAMLResponse" value="{SAML_ASSERTION}" />'
                '<input type="hidden" name="RelayState" value="hea" />'
                '<noscript><input type="submit" value="Continue" /></noscript>'
                "</form></body></html>",
            )
        if path == "/portal/sso/opower":
            return _redirect(f"https://wgl.opower.com/ei/app/r/sso?code={SSO_CODE}")
        return FakeResponse(404, "not found")

    def _dashboard(self, method: str, data: dict[str, str]) -> FakeResponse:
        mode = self.portal_mode
        if method == "POST":
            assert mode == "postback" and data.get("__EVENTTARGET") == "ctl00$Nav$lnkEnergy"
            assert data.get("__VIEWSTATE") == "vs-dash"
            return _redirect(f"https://wgl.opower.com/ei/app/sso?token={SSO_CODE}")
        links = ['<a href="/portal/Pay.aspx">Pay My Bill</a>', '<a href="/portal/Logout.aspx">Sign Out</a>']
        if mode in ("saml", "token", "bad_assertion"):
            links.append('<a href="/portal/Usage.aspx">Usage</a>')
        elif mode == "postback":
            links.append("<a href=\"javascript:__doPostBack('ctl00$Nav$lnkEnergy','')\">Home Energy Analysis</a>")
        elif mode == "redirect":
            links.append('<a href="https://my.washingtongas.com/portal/sso/opower">Home Energy Analysis</a>')
        elif mode == "opower_wall":
            # A plain link with no SSO behind it: Opower bounces it to its own sign-in page.
            links.append('<a href="https://wgl.opower.com/ei/x/home-energy-analysis">Home Energy Analysis</a>')
        return FakeResponse(
            200,
            '<html><body><form method="post" action="Dashboard.aspx">'
            '<input type="hidden" name="__VIEWSTATE" value="vs-dash" />'
            '<input type="hidden" name="__EVENTTARGET" value="" />'
            '<input type="hidden" name="__EVENTARGUMENT" value="" />'
            f"{''.join(links)}</form></body></html>",
        )

    # Opower --------------------------------------------------------------

    def _route(
        self,
        method: str,
        url: str,
        params: dict[str, str],
        body: Any,
        form: dict[str, str],
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
        if path == f"/ei/edge/apis/user-account-control-v1/cws/v1/{subdomain}/account/signin":
            if self.login_status is not None:
                return FakeResponse(self.login_status, "")
            if self.direct_login_works and body == {"username": USERNAME, "password": PASSWORD}:
                self.signed_in.add(subdomain)
                return FakeResponse(204)
            return FakeResponse(401, "")
        # Hand-offs from My Washington Gas.
        if path == "/ei/sso/saml/acs" and method == "POST":
            if self.portal_mode == "bad_assertion":
                # Lands on a public Opower page without starting a session.
                return _redirect("/ei/x/welcome")
            if form.get("SAMLResponse") == SAML_ASSERTION:
                self.signed_in.add(subdomain)
            return _redirect("/ei/x/home-energy-analysis")
        if path == "/ei/x/welcome":
            return FakeResponse(200, "<html><body>Welcome</body></html>")
        if path in ("/ei/app/sso", "/ei/app/r/sso"):
            if SSO_CODE in (params.get("token"), params.get("code")) or SSO_CODE in parts.query:
                self.signed_in.add(subdomain)
            return _redirect("/ei/x/home-energy-analysis")
        if path == "/ei/x/home-energy-analysis":
            if subdomain not in self.signed_in:
                return _redirect("/ei/x/sign-in-wall")
            return FakeResponse(200, "<html><body>Home Energy Analysis</body></html>")
        if subdomain not in self.signed_in and headers.get("Authorization") != f"Bearer {EMBEDDED_TOKEN}":
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
