"""Check that your Washington Gas login works and see what data comes back.

This uses the same code as the Home Assistant integration, without needing
Home Assistant. It needs Python 3.11+ and aiohttp (already there if you run
it inside the Home Assistant container):

    pip install aiohttp
    python3 scripts/check_connection.py

It asks for your email and password and never saves them. It reports each
sign-in step (My Washington Gas sign-in, hand-off to Opower, Opower API) with
the host and HTTP status where it stopped. Passwords, cookies, tokens and
query strings are never printed, and account numbers are partly hidden, so
the output is safe to paste into a GitHub issue.

    --trace   also list every request (method, host, path, status) when it works
    --debug   Python debug logging
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import getpass
import importlib
import logging
from pathlib import Path
import sys
import types
from urllib.parse import urlsplit

import aiohttp

# Load the integration's client as a package, without its Home Assistant parts.
_PACKAGE_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "washington_gas"
_package = types.ModuleType("washington_gas_client")
_package.__path__ = [str(_PACKAGE_DIR)]
sys.modules["washington_gas_client"] = _package
api = importlib.import_module("washington_gas_client.api")
errors = importlib.import_module("washington_gas_client.errors")


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
        print(f"  {mark} {errors.STEP_NAMES.get(attempt.step, attempt.step)}: {attempt.detail}{suffix}")


def _print_trace(client: api.WashingtonGasClient) -> None:
    if not client.trace:
        return
    print("\nRequests made (hosts and paths only):")
    for line in client.trace:
        print(f"  {line}")


async def _login(client: api.WashingtonGasClient) -> bool:
    try:
        await client.async_login()
    except api.MfaRequired as err:
        _print_steps(client)
        print(f"\nStopped: {err}.")
        print(
            "The integration can't sign in to an account that asks for a code at sign-in. "
            "It doesn't try to get around this."
        )
        return False
    except api.CaptchaRequired as err:
        _print_steps(client)
        print(f"\nStopped: {err}.")
        print("The integration doesn't try to get around CAPTCHAs.")
        return False
    except api.InvalidAuth:
        _print_steps(client)
        print("\nThe email and password were rejected. Check they work at https://my.washingtongas.com")
        return False
    except api.NoAccounts:
        _print_steps(client)
        print("\nThe login worked, but no gas accounts are linked to it.")
        return False
    except api.CannotConnect as err:
        _print_steps(client)
        step = getattr(err, "step", None)
        where = f" at {errors.STEP_NAMES.get(step, step)}" if step else ""
        print(f"\nStopped{where}: {err}")
        _print_trace(client)
        print("\nIf you open an issue, paste everything above. It has no passwords, cookies or tokens.")
        return False
    _print_steps(client)
    return True


async def _report(session: aiohttp.ClientSession, username: str, password: str, trace: bool) -> int:
    client = api.WashingtonGasClient(session, username, password)
    if not await _login(client):
        return 1
    if trace:
        _print_trace(client)
    print(f"\nUsing {client.portal.subdomain}.opower.com (signed in via {client.login_method})")

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
        print(f"\nFAILED Opower API: signed in, but reading data failed [{urlsplit(err.url).hostname}, {status}]")
        return 1
    return 0


async def _run(username: str, password: str, trace: bool) -> int:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(quote_cookie=False)) as session:
        return await _report(session, username, password, trace)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trace", action="store_true", help="list every request even when it works")
    parser.add_argument("--debug", action="store_true", help="Python debug logging")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    username = input("My Washington Gas email or username: ").strip()
    password = getpass.getpass("Password: ")
    return asyncio.run(_run(username, password, args.trace))


if __name__ == "__main__":
    sys.exit(main())
