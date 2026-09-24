"""Check that your Washington Gas login works and see what data comes back.

This uses the same code as the Home Assistant integration, without needing
Home Assistant. It needs Python 3.11+ and aiohttp:

    pip install aiohttp
    python3 scripts/check_connection.py

It asks for your username and password and never saves them. Account
numbers are partly hidden in the output so it is safe to paste into an issue.
Add --debug to see each request's status.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import getpass
import importlib.util
import logging
from pathlib import Path
import sys

import aiohttp

# Load api.py by path so Home Assistant isn't needed.
_API_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "washington_gas" / "api.py"
_spec = importlib.util.spec_from_file_location("washington_gas_api", _API_PATH)
assert _spec and _spec.loader
api = importlib.util.module_from_spec(_spec)
sys.modules["washington_gas_api"] = api
_spec.loader.exec_module(api)


def _mask(value: str) -> str:
    return "*" * max(0, len(value) - 4) + value[-4:]


def _amount(value: float | None, unit: str) -> str:
    if value is None:
        return "unknown"
    return f"${value:.2f}" if unit == "$" else f"{value:g} {unit}"


async def _run(username: str, password: str) -> int:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(quote_cookie=False)) as session:
        try:
            return await _report(session, username, password)
        except api.ApiError as err:
            print(f"\nLogged in, but reading data failed: {err}")
            return 1


async def _report(session: aiohttp.ClientSession, username: str, password: str) -> int:
    client = api.WashingtonGasClient(session, username, password)
    try:
        await client.async_login()
    except api.InvalidAuth:
        print("Login rejected. Check the username and password work at https://wgl.opower.com")
        return 1
    except api.NoAccounts:
        print("Login worked, but no gas accounts are linked to it.")
        return 1
    except api.CannotConnect as err:
        print(f"Could not connect: {err}")
        return 1
    print(f"Logged in on {client.portal.subdomain}.opower.com")

    accounts = await client.async_get_accounts()
    forecasts = await client.async_get_forecasts(accounts)
    today = datetime.now(api.TIMEZONE).date()
    for account in accounts:
        print(
            f"\nAccount {_mask(account.utility_account_id)}: "
            f"{account.meter_type}, reads by {account.read_resolution.lower()}"
        )
        forecast = forecasts.get(account.uuid)
        unit = "CCF" if forecast and forecast.unit == "CCF" else "therms"
        if forecast:
            print(
                f"  This bill ({forecast.start_date} to {forecast.end_date}): "
                f"{_amount(forecast.usage_to_date, unit)} so far, {_amount(forecast.cost_to_date, '$')}; "
                f"forecast {_amount(forecast.forecasted_usage, unit)}, {_amount(forecast.forecasted_cost, '$')}"
            )
        else:
            print("  No bill forecast available")
        if account.supports("day"):
            days = await client.async_get_cost_reads(account, "day", today - timedelta(days=10), today)
            print(f"  Last {len(days)} days:")
            for read in days:
                print(f"    {read.start_time.date()}: {_amount(read.consumption, unit)}, {_amount(read.cost, '$')}")
        bills = await client.async_get_cost_reads(account, "bill")
        print(f"  {len(bills)} bills on record")
        for read in bills[-3:]:
            print(
                f"    {read.start_time.date()} to {read.end_time.date()}: "
                f"{_amount(read.consumption, unit)}, {_amount(read.cost, '$')}"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--debug", action="store_true", help="log each request")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    username = input("Washington Gas username or email: ").strip()
    password = getpass.getpass("Password: ")
    return asyncio.run(_run(username, password))


if __name__ == "__main__":
    sys.exit(main())
