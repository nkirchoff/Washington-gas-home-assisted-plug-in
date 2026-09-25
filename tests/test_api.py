"""Tests for the Opower client."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise

import pytest

from custom_components.washington_gas.api import (
    CannotConnect,
    InvalidAuth,
    Portal,
    WashingtonGasClient,
)
from custom_components.washington_gas.coordinator import _merge_reads

from .fake_opower import ACCOUNT_NUMBER, PASSWORD, TZ, USERNAME, FakeOpower, build_daily


def _client(site: FakeOpower, password: str = PASSWORD, portal: Portal | None = None) -> WashingtonGasClient:
    return WashingtonGasClient(site, USERNAME, password, portal)  # type: ignore[arg-type]


async def test_login_finds_wgl_portal() -> None:
    """The first portal is used when it accepts the login."""
    site = FakeOpower.with_history()
    client = _client(site)
    await client.async_login()
    assert client.portal == Portal("wgl", "wgl")
    accounts = await client.async_get_accounts()
    assert len(accounts) == 1
    assert accounts[0].account_id == ACCOUNT_NUMBER
    assert accounts[0].read_resolution == "DAY"
    assert accounts[0].supports("day")
    assert not accounts[0].supports("hour")


async def test_login_falls_back_to_second_portal() -> None:
    """When wgl doesn't exist, wglm is tried."""
    site = FakeOpower.with_history(subdomains={"wglm"})
    client = _client(site)
    await client.async_login()
    assert client.portal == Portal("wglm", "wglm")


async def test_bad_password() -> None:
    """A rejected login raises InvalidAuth."""
    client = _client(FakeOpower.with_history(), password="wrong")
    with pytest.raises(InvalidAuth):
        await client.async_login()
    assert client.portal is None


async def test_bad_password_known_portal() -> None:
    """A rejected login on a remembered portal raises InvalidAuth."""
    client = _client(FakeOpower.with_history(), password="wrong", portal=Portal("wgl", "wgl"))
    with pytest.raises(InvalidAuth):
        await client.async_login()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_server_errors_are_not_auth_errors(status: int) -> None:
    """Throttling and outages must not ask the user for a new password."""
    client = _client(FakeOpower.with_history(login_status=status))
    with pytest.raises(CannotConnect):
        await client.async_login()


async def test_no_portal_exists() -> None:
    """If neither site exists, the error is a connection error."""
    client = _client(FakeOpower.with_history(subdomains=set()))
    with pytest.raises(CannotConnect):
        await client.async_login()


async def test_graphql_forecast() -> None:
    """The bill forecast is read from GraphQL."""
    client = _client(FakeOpower.with_history())
    await client.async_login()
    accounts = await client.async_get_accounts()
    forecast = (await client.async_get_forecasts(accounts))[accounts[0].uuid]
    assert forecast.unit == "THERM"
    assert forecast.usage_to_date == 48.0
    assert forecast.cost_to_date == 91.2
    assert forecast.forecasted_usage == 95.0
    assert forecast.typical_cost == 170.0
    assert forecast.start_date.isoformat() == "2026-01-05"
    assert forecast.end_date.isoformat() == "2026-02-04"


async def test_rest_forecast_fallback() -> None:
    """If GraphQL fails, the older forecast endpoint is used."""
    client = _client(FakeOpower.with_history(forecast_mode="rest"))
    await client.async_login()
    accounts = await client.async_get_accounts()
    forecast = (await client.async_get_forecasts(accounts))[accounts[0].uuid]
    assert forecast.usage_to_date == 40.0
    assert forecast.forecasted_cost == 170.0


async def test_no_forecast() -> None:
    """No forecast anywhere is not an error."""
    client = _client(FakeOpower.with_history(forecast_mode=None))
    await client.async_login()
    accounts = await client.async_get_accounts()
    assert await client.async_get_forecasts(accounts) == {}


async def test_daily_reads_span_chunks_without_gaps() -> None:
    """Long ranges are split into chunks with no missing or repeated days."""
    site = FakeOpower.with_history()
    client = _client(site)
    await client.async_login()
    account = (await client.async_get_accounts())[0]
    today = datetime.now(TZ).date()
    reads = await client.async_get_cost_reads(account, "day", today - timedelta(days=3 * 365), today)
    assert [read.start_time for read in reads] == [read.start for read in site.daily]
    assert [read.consumption for read in reads] == [read.therms for read in site.daily]
    assert reads[0].cost == site.daily[0].cost
    day_requests = [call for call in site.calls if call[2].get("aggregateType") == "day"]
    # 400 days of data needs two 363 day chunks, then one empty chunk ends the walk.
    assert len(day_requests) == 3


async def test_usage_only_fallback() -> None:
    """If the cost endpoint fails for daily reads, usage still comes through."""
    site = FakeOpower.with_history(cost_endpoint_fails=True)
    client = _client(site)
    await client.async_login()
    account = (await client.async_get_accounts())[0]
    today = datetime.now(TZ).date()
    reads = await client.async_get_cost_reads(account, "day", today - timedelta(days=10), today)
    assert [read.consumption for read in reads] == [read.therms for read in site.daily[-9:]]
    assert all(read.cost == 0 for read in reads)


async def test_trailing_zero_reads_are_dropped() -> None:
    """Days that aren't published yet come back as zeros and are ignored."""
    site = FakeOpower.with_history()
    today = datetime.now(TZ).date()
    for read in build_daily(today - timedelta(days=1), today):
        read.therms = 0
        read.cost = 0
        site.daily.append(read)
    client = _client(site)
    await client.async_login()
    account = (await client.async_get_accounts())[0]
    reads = await client.async_get_cost_reads(account, "day", today - timedelta(days=5), today)
    assert reads[-1].start_time.date() == today - timedelta(days=2)


async def test_bill_only_account_rejects_daily() -> None:
    """Asking for daily reads on a bill-only account is a programming error."""
    client = _client(FakeOpower.with_history(read_resolution="BILLING"))
    await client.async_login()
    account = (await client.async_get_accounts())[0]
    today = datetime.now(TZ).date()
    with pytest.raises(ValueError):
        await client.async_get_cost_reads(account, "day", today, today)


def test_merge_keeps_each_moment_once() -> None:
    """Bills are kept only before the daily reads start. A straddling bill wins its period."""
    site = FakeOpower.with_history()
    from custom_components.washington_gas.api import CostRead

    def convert(reads):
        return [CostRead(r.start, r.end, r.therms, r.cost) for r in reads]

    merged = _merge_reads(convert(site.bills), convert(site.daily))
    expected = site.expected_history()
    assert [read.start_time for read in merged] == [read.start for read in expected]
    # Periods never overlap.
    for earlier, later in pairwise(merged):
        assert earlier.end_time <= later.start_time
