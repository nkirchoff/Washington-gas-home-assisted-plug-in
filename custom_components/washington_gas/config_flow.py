"""Config flow for the Washington Gas integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import CannotConnect, InvalidAuth, NoAccounts, Portal, WashingtonGasClient
from .const import (
    CONF_ENERGY_UNIT,
    CONF_PORTAL_SUBDOMAIN,
    CONF_PORTAL_UTILITY_CODE,
    DEFAULT_ENERGY_UNIT,
    DOMAIN,
    ENERGY_UNIT_CCF,
    ENERGY_UNIT_KWH,
)
from .coordinator import create_session

_LOGGER = logging.getLogger(__name__)

_USERNAME_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username"))
_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): _USERNAME_SELECTOR,
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
        vol.Required(CONF_ENERGY_UNIT, default=DEFAULT_ENERGY_UNIT): SelectSelector(
            SelectSelectorConfig(
                options=[ENERGY_UNIT_CCF, ENERGY_UNIT_KWH],
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_ENERGY_UNIT,
            )
        ),
    }
)
STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR})


async def async_validate_login(hass: HomeAssistant, username: str, password: str) -> Portal:
    """Log in, check there is at least one account, and return the Opower site used."""
    client = WashingtonGasClient(create_session(hass), username, password)
    await client.async_login()
    if not await client.async_get_accounts() or client.portal is None:
        raise NoAccounts
    return client.portal


class WashingtonGasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Washington Gas."""

    VERSION = 1

    async def _async_check(self, username: str, password: str, errors: dict[str, str]) -> Portal | None:
        """Validate a login, filling errors on failure."""
        try:
            return await async_validate_login(self.hass, username, password)
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except NoAccounts:
            errors["base"] = "no_accounts"
        except Exception:
            _LOGGER.exception("Unexpected error while checking the Washington Gas login")
            errors["base"] = "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the Washington Gas login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            portal = await self._async_check(username, user_input[CONF_PASSWORD], errors)
            if portal is not None:
                return self.async_create_entry(
                    title=f"Washington Gas ({username})",
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_ENERGY_UNIT: user_input[CONF_ENERGY_UNIT],
                        CONF_PORTAL_SUBDOMAIN: portal.subdomain,
                        CONF_PORTAL_UTILITY_CODE: portal.utility_code,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after the saved password stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the new password."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            portal = await self._async_check(entry.data[CONF_USERNAME], user_input[CONF_PASSWORD], errors)
            if portal is not None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_PORTAL_SUBDOMAIN: portal.subdomain,
                        CONF_PORTAL_UTILITY_CODE: portal.utility_code,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
        )
