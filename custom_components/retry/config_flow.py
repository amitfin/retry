"""Config flow for retry integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlowResult

from .const import CONF_DISABLE_INITIAL_CHECK, CONF_DISABLE_REPAIR, DOMAIN


class RetryConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is None:
            return self.async_show_form(step_id="user")

        return await self.async_step_import()

    async def async_step_import(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Occurs when an entry is setup through config."""
        return self.async_create_entry(
            title=DOMAIN.title(),
            data={},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlow):
    """Handles options flow for the component."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Handle an options flow."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data=user_input,
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DISABLE_INITIAL_CHECK,
                        default=self._config_entry.options.get(
                            CONF_DISABLE_INITIAL_CHECK, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_DISABLE_REPAIR,
                        default=self._config_entry.options.get(
                            CONF_DISABLE_REPAIR, False
                        ),
                    ): selector.BooleanSelector(),
                },
            ),
        )
