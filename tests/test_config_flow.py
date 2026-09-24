"""Tests for the config flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.washington_gas.const import (
    CONF_ENERGY_UNIT,
    CONF_PORTAL_SUBDOMAIN,
    CONF_PORTAL_UTILITY_CODE,
    DOMAIN,
    ENERGY_UNIT_KWH,
)

from .fake_opower import PASSWORD, USERNAME, FakeOpower


@pytest.fixture(autouse=True)
def no_setup():
    """Don't set the entry up after the flow; that's covered elsewhere."""
    with patch("custom_components.washington_gas.async_setup_entry", return_value=True) as mock:
        yield mock


async def test_user_flow(hass: HomeAssistant, mock_session: FakeOpower) -> None:
    """A working login creates the entry and remembers the portal."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: f"  {USERNAME} ", CONF_PASSWORD: PASSWORD, CONF_ENERGY_UNIT: ENERGY_UNIT_KWH},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Washington Gas ({USERNAME})"
    assert result["data"] == {
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: PASSWORD,
        CONF_ENERGY_UNIT: ENERGY_UNIT_KWH,
        CONF_PORTAL_SUBDOMAIN: "wgl",
        CONF_PORTAL_UTILITY_CODE: "wgl",
    }
    assert result["result"].unique_id == USERNAME


@pytest.mark.parametrize(
    ("site_kwargs", "password", "error"),
    [
        ({}, "wrong", "invalid_auth"),
        ({"login_status": 503}, PASSWORD, "cannot_connect"),
        ({"subdomains": set()}, PASSWORD, "cannot_connect"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, mock_session: FakeOpower, site_kwargs: dict, password: str, error: str
) -> None:
    """Errors are shown, and the flow recovers once the problem is fixed."""
    for key, value in site_kwargs.items():
        setattr(mock_session, key, value)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: password, CONF_ENERGY_UNIT: "ccf"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    fresh = FakeOpower.with_history()
    for key in ("subdomains", "login_status"):
        setattr(mock_session, key, getattr(fresh, key))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD, CONF_ENERGY_UNIT: "ccf"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_no_accounts(hass: HomeAssistant, mock_session: FakeOpower) -> None:
    """A login with no accounts behind it is reported clearly."""
    with patch.object(FakeOpower, "_customers", return_value={"customers": []}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD, CONF_ENERGY_UNIT: "ccf"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_accounts"}


async def test_user_flow_unexpected_error(hass: HomeAssistant, mock_session: FakeOpower) -> None:
    """A bug shows the generic error instead of crashing the flow."""
    with patch("custom_components.washington_gas.config_flow.async_validate_login", side_effect=RuntimeError("boom")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD, CONF_ENERGY_UNIT: "ccf"}
        )
    assert result["errors"] == {"base": "unknown"}


async def test_already_configured(hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry) -> None:
    """The same login can't be added twice."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME.upper(), CONF_PASSWORD: PASSWORD, CONF_ENERGY_UNIT: "ccf"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, mock_session: FakeOpower, config_entry: MockConfigEntry) -> None:
    """A new password replaces the old one."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, CONF_PASSWORD: "old"})
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "still-wrong"})
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: PASSWORD})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == PASSWORD
