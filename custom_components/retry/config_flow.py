"""Config flow for retry integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

if TYPE_CHECKING:
    import probatio
    from homeassistant.config_entries import ConfigFlowResult

from .const import CONF_DISABLE_INITIAL_CHECK, CONF_DISABLE_REPAIR, DOMAIN


class RetryConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
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
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:  # noqa: ARG004
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Handles options flow for the component."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle an options flow."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data=user_input,
            )

        return self.async_show_form(
            step_id="init",
            # HA 2026.9+ annotates schemas with probatio types.
            data_schema=cast(
                "probatio.Schema",
                vol.Schema(
                    {
                        vol.Required(
                            CONF_DISABLE_INITIAL_CHECK,
                            default=self.config_entry.options.get(
                                CONF_DISABLE_INITIAL_CHECK, False
                            ),
                        ): selector.BooleanSelector(),
                        vol.Required(
                            CONF_DISABLE_REPAIR,
                            default=self.config_entry.options.get(
                                CONF_DISABLE_REPAIR, False
                            ),
                        ): selector.BooleanSelector(),
                    },
                ),
            ),
        )
