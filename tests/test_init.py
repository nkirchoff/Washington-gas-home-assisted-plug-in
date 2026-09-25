"""Tests for setup, sensors and the Energy dashboard history."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import get_last_statistics, list_statistic_ids
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.washington_gas.const import (
    CONF_ENERGY_UNIT,
    CONF_LOGIN_METHOD,
    CONF_PORTAL_SUBDOMAIN,
    CONF_PORTAL_UTILITY_CODE,
    ENERGY_UNIT_KWH,
    KWH_PER_THERM,
)
from custom_components.washington_gas.diagnostics import async_get_config_entry_diagnostics

from .fake_opower import ACCOUNT_NUMBER, TZ, FakeOpower, build_bills, build_daily

USAGE_ID = f"washington_gas:{ACCOUNT_NUMBER}_gas_usage"
USAGE_KWH_ID = f"washington_gas:{ACCOUNT_NUMBER}_gas_usage_kwh"
COST_ID = f"washington_gas:{ACCOUNT_NUMBER}_gas_cost"
PREFIX = f"sensor.washington_gas_{ACCOUNT_NUMBER}"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def _last(hass: HomeAssistant, statistic_id: str) -> dict[str, Any]:
    stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum", "state"}
    )
    return stats[statistic_id][0]


async def _all_stats(hass: HomeAssistant, statistic_id: str) -> list[dict[str, Any]]:
    stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 100_000, statistic_id, True, {"sum", "state"}
    )
    return list(reversed(stats[statistic_id]))


async def test_setup_creates_sensors(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """Sensors show the forecast, the latest day and the last bill."""
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    state = hass.states.get(f"{PREFIX}_current_bill_gas_usage")
    assert state.state == "48.0"
    assert state.attributes["unit_of_measurement"] == "therms"
    assert hass.states.get(f"{PREFIX}_current_bill_gas_cost").state == "91.2"
    assert hass.states.get(f"{PREFIX}_forecasted_bill_gas_usage").state == "95.0"
    assert hass.states.get(f"{PREFIX}_forecasted_bill_gas_cost").state == "180.5"
    assert hass.states.get(f"{PREFIX}_typical_bill_gas_usage").state == "102.0"
    assert hass.states.get(f"{PREFIX}_typical_bill_gas_cost").state == "170.0"
    assert hass.states.get(f"{PREFIX}_bill_period_start").state == "2026-01-05"
    assert hass.states.get(f"{PREFIX}_bill_period_end").state == "2026-02-04"

    latest = mock_session.daily[-1]
    state = hass.states.get(f"{PREFIX}_latest_daily_gas_usage")
    assert float(state.state) == latest.therms
    assert state.attributes["period_start"] == latest.start.date().isoformat()
    assert float(hass.states.get(f"{PREFIX}_latest_daily_gas_cost").state) == latest.cost

    last_bill = mock_session.bills[-1]
    state = hass.states.get(f"{PREFIX}_last_bill_gas_usage")
    assert float(state.state) == last_bill.therms
    assert state.attributes["period_end"] == last_bill.end.date().isoformat()
    assert float(hass.states.get(f"{PREFIX}_last_bill_gas_cost").state) == pytest.approx(last_bill.cost)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_history_import(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """The first update imports bills, then daily reads, each moment once."""
    await _setup(hass, config_entry)

    expected = mock_session.expected_history()
    usage = await _all_stats(hass, USAGE_ID)
    assert [datetime.fromtimestamp(row["start"], TZ) for row in usage] == [read.start for read in expected]
    assert usage[-1]["sum"] == pytest.approx(sum(read.therms for read in expected))
    cost = await _last(hass, COST_ID)
    assert cost["sum"] == pytest.approx(sum(read.cost for read in expected))

    ids = await get_instance(hass).async_add_executor_job(list_statistic_ids, hass)
    units = {row["statistic_id"]: row["statistics_unit_of_measurement"] for row in ids}
    assert units[USAGE_ID] == "CCF"


async def test_incremental_update(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """Later updates add only new days and keep the running total."""
    await _setup(hass, config_entry)
    before = await _last(hass, USAGE_ID)

    today = datetime.now(TZ).date()
    new_days = build_daily(today - timedelta(days=1), today - timedelta(days=1))
    mock_session.daily.extend(new_days)
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    after = await _last(hass, USAGE_ID)
    assert datetime.fromtimestamp(after["start"], TZ) == new_days[0].start
    assert after["sum"] == pytest.approx(before["sum"] + new_days[0].therms)

    # Refreshing again with nothing new changes nothing.
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    assert await _last(hass, USAGE_ID) == after


async def test_incremental_update_with_new_bill(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """A new bill whose days are already stored is not counted again."""
    await _setup(hass, config_entry)
    before = await _last(hass, USAGE_ID)
    # A bill for a period whose days are already stored, plus one new day.
    new_bill = build_bills(mock_session.bills[-1].end.date(), mock_session.bills[-1].end.date() + timedelta(days=40))
    assert len(new_bill) == 1
    mock_session.bills.extend(new_bill)
    next_day = mock_session.daily[-1].end.date()
    new_days = build_daily(next_day, next_day)
    mock_session.daily.extend(new_days)

    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    after = await _last(hass, USAGE_ID)
    assert after["sum"] == pytest.approx(before["sum"] + new_days[0].therms)


async def test_kwh_option(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """The kWh option converts therms exactly and uses its own statistic."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, CONF_ENERGY_UNIT: ENERGY_UNIT_KWH})
    await _setup(hass, config_entry)

    expected = mock_session.expected_history()
    last = await _last(hass, USAGE_KWH_ID)
    assert last["sum"] == pytest.approx(sum(read.therms for read in expected) * KWH_PER_THERM)
    ids = await get_instance(hass).async_add_executor_job(list_statistic_ids, hass)
    units = {row["statistic_id"]: row["statistics_unit_of_measurement"] for row in ids}
    assert units[USAGE_KWH_ID] == "kWh"
    assert USAGE_ID not in units
    # Sensors still show therms.
    assert hass.states.get(f"{PREFIX}_current_bill_gas_usage").attributes["unit_of_measurement"] == "therms"


async def test_bill_only_account(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """Accounts without daily reads still get bill history and bill sensors."""
    mock_session.read_resolution = "BILLING"
    await _setup(hass, config_entry)

    assert hass.states.get(f"{PREFIX}_latest_daily_gas_usage") is None
    assert hass.states.get(f"{PREFIX}_last_bill_gas_usage") is not None
    last = await _last(hass, USAGE_ID)
    assert last["sum"] == pytest.approx(sum(read.therms for read in mock_session.bills))


async def test_no_forecast(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """Without a forecast, the forecast sensors are left out."""
    mock_session.forecast_mode = None
    await _setup(hass, config_entry)
    assert hass.states.get(f"{PREFIX}_current_bill_gas_usage") is None
    assert hass.states.get(f"{PREFIX}_latest_daily_gas_usage") is not None


async def test_portal_is_remembered(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """If the saved portal is missing, the one that works is found and saved."""
    mock_session.subdomains = {"wglm"}
    data = dict(config_entry.data)
    del data[CONF_PORTAL_SUBDOMAIN]
    del data[CONF_PORTAL_UTILITY_CODE]
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data=data)
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data[CONF_PORTAL_SUBDOMAIN] == "wglm"
    assert config_entry.data[CONF_PORTAL_UTILITY_CODE] == "wglm"


async def test_setup_through_my_washington_gas(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """An entry that signs in through My Washington Gas loads, keeps using that path, and imports history."""
    mock_session.portal_mode = "saml"
    mock_session.direct_login_works = False
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, CONF_LOGIN_METHOD: "washingtongas"})
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(f"{PREFIX}_current_bill_gas_usage").state == "48.0"
    assert (await _last(hass, USAGE_ID))["sum"] > 0
    assert not any("account/signin" in url for _, url, _ in mock_session.calls)

    result = await async_get_config_entry_diagnostics(hass, config_entry)
    assert result["login_method"] == "washingtongas"
    assert [step["step"] for step in result["login_report"]] == ["washingtongas_login", "handoff", "opower_api"]
    assert "correct-horse" not in str(result)


async def test_old_entry_learns_login_method(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """An entry from before the My Washington Gas path saves whichever login works."""
    mock_session.portal_mode = "saml"
    mock_session.direct_login_works = False
    await _setup(hass, config_entry)
    assert config_entry.data[CONF_LOGIN_METHOD] == "washingtongas"


async def test_mfa_after_setup_retries_without_reauth(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """A verification code prompt isn't a wrong password, so it doesn't ask for a new one."""
    mock_session.portal_mode = "mfa"
    mock_session.direct_login_works = False
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, CONF_LOGIN_METHOD: "washingtongas"})
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not any(flow["context"]["source"] == SOURCE_REAUTH for flow in hass.config_entries.flow.async_progress())


async def test_bad_password_starts_reauth(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """A rejected password asks the user for a new one."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, CONF_PASSWORD: "changed"})
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)


async def test_outage_retries(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """A Washington Gas outage retries setup later instead of failing for good."""
    mock_session.login_status = 503
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_diagnostics_hide_secrets(
    recorder_mock: Recorder, hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry
) -> None:
    """Diagnostics never include the password or account numbers."""
    await _setup(hass, config_entry)
    result = await async_get_config_entry_diagnostics(hass, config_entry)
    text = str(result)
    assert "correct-horse" not in text
    assert ACCOUNT_NUMBER not in text
    assert "user@example.com" not in text
    assert result["portal"] == {"subdomain": "wgl", "utility_code": "wgl"}
    assert result["accounts"][0]["unit"] == "THERM"
