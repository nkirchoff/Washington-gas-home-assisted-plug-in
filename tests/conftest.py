"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

from homeassistant.components.recorder import Recorder
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.washington_gas.const import (
    CONF_ENERGY_UNIT,
    CONF_PORTAL_SUBDOMAIN,
    CONF_PORTAL_UTILITY_CODE,
    DOMAIN,
    ENERGY_UNIT_CCF,
)

from .fake_opower import PASSWORD, USERNAME, FakeOpower


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: Recorder, enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration from custom_components.

    The recorder is started first because the integration depends on it.
    """


@pytest.fixture
def site() -> FakeOpower:
    """The fake Washington Gas site with a few years of history."""
    return FakeOpower.with_history()


@pytest.fixture
def mock_session(site: FakeOpower) -> Generator[FakeOpower]:
    """Send every request from the integration to the fake site."""
    with (
        patch("custom_components.washington_gas.coordinator.create_session", return_value=site),
        patch("custom_components.washington_gas.config_flow.create_session", return_value=site),
    ):
        yield site


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A config entry as the config flow creates it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Washington Gas ({USERNAME})",
        unique_id=USERNAME,
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_ENERGY_UNIT: ENERGY_UNIT_CCF,
            CONF_PORTAL_SUBDOMAIN: "wgl",
            CONF_PORTAL_UTILITY_CODE: "wgl",
        },
    )
