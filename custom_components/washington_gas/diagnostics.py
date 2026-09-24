"""Diagnostics for the Washington Gas integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import WashingtonGasConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, "customer_uuid", "uuid", "utility_account_id", "account_id"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: WashingtonGasConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry, without credentials or account numbers."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "portal": asdict(coordinator.client.portal) if coordinator.client.portal else None,
        "last_update_success": coordinator.last_update_success,
        "accounts": [async_redact_data(asdict(data), TO_REDACT) for data in (coordinator.data or {}).values()],
    }
