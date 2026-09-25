"""Check that your Washington Gas login works and see what data comes back.

This uses the same code as the Home Assistant integration, without needing
Home Assistant. It needs Python 3.11+ and aiohttp (already there if you run
it inside the Home Assistant container):

    pip install aiohttp
    python3 scripts/check_connection.py

Use your wgl.opower.com login. A My Washington Gas (my.washingtongas.com)
login won't work there. If you don't have a wgl.opower.com login, create one
at https://wgl.opower.com/ei/x/create-account first.

It asks for your email and password and never saves them. It reports each
sign-in step with the host and HTTP status where it stopped. Passwords,
cookies and tokens are never printed, and account numbers are partly hidden,
so the output is safe to paste into a GitHub issue.

    --debug   Python debug logging
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
from urllib.parse import urlsplit

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


def _print_steps(client: api.WashingtonGasClient) -> None:
    print("Sign-in steps:")
    for attempt in client.login_report:
        where = []
        if attempt.host:
            where.append(attempt.host)
        if attempt.status is not None:
            where.append(f"HTTP {attempt.status}")
        suffix = f" [{', '.join(where)}]" if where else ""
        mark = "OK    " if attempt.ok else "FAILED"
        print(f"  {mark} {api.STEP_NAMES.get(attempt.step, attempt.step)}: {attempt.detail}{suffix}")


async def _login(client: api.WashingtonGasClient) -> bool:
    try:
        await client.async_login()
    except api.InvalidAuth:
        _print_steps(client)
        print("\nThe email and password were rejected. Check they work at https://wgl.opower.com")
        print(
            "A My Washington Gas login won't work there. "
            f"If you don't have a wgl.opower.com login, create one at {api.CREATE_ACCOUNT_URL}"
        )
        return False
    except api.NoAccounts:
        _print_steps(client)
        print("\nThe login worked, but no gas accounts are linked to it.")
        return False
    except api.CannotConnect as err:
        _print_steps(client)
        print(f"\nStopped: {err}")
        print("\nIf you open an issue, paste everything above. It has no passwords, cookies or tokens.")
        return False
    _print_steps(client)
    return True


async def _report(session: aiohttp.ClientSession, username: str, password: str) -> int:
    client = api.WashingtonGasClient(session, username, password)
    if not await _login(client):
        return 1
    print(f"\nUsing {client.portal.subdomain}.opower.com")

    try:
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
    except api.ApiError as err:
        status = f"HTTP {err.status}" if err.status is not None else "no response"
        print(f"\nFAILED Reading data: signed in, but a data request failed [{urlsplit(err.url).hostname}, {status}]")
        return 1
    return 0


async def _run(username: str, password: str) -> int:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(quote_cookie=False)) as session:
        return await _report(session, username, password)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--debug", action="store_true", help="Python debug logging")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    username = input("wgl.opower.com email or username: ").strip()
    password = getpass.getpass("Password: ")
    return asyncio.run(_run(username, password))


if __name__ == "__main__":
    sys.exit(main())
