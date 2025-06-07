"""Tests for the retry config flow."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.retry.const import (
    CONF_DISABLE_INITIAL_CHECK,
    CONF_DISABLE_REPAIR,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_form(hass: HomeAssistant) -> None:
    """Test a user form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result.get("type") == FlowResultType.FORM


async def test_simple(hass: HomeAssistant) -> None:
    """Test a simple user flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data={}
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    assert result.get("title") == DOMAIN.title()


async def test_already_setup(hass: HomeAssistant) -> None:
    """Test we abort if already setup."""
    MockConfigEntry(
        domain=DOMAIN,
        data={},
    ).add_to_hass(hass)

    # Should fail, same DOMAIN
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result.get("type") == FlowResultType.ABORT
    assert result.get("reason") == "single_instance_allowed"


async def test_import(hass: HomeAssistant) -> None:
    """Test import from configuration.yaml."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    assert result.get("title") == DOMAIN.title()


async def test_options_flow(hass: HomeAssistant) -> None:
    """Test the options flow."""
    config_entry = MockConfigEntry(domain=DOMAIN, data={})
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "init"

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_DISABLE_INITIAL_CHECK: True, CONF_DISABLE_REPAIR: True},
    )
    assert result2.get("type") == FlowResultType.CREATE_ENTRY
    assert result2.get("data", {}).get(CONF_DISABLE_INITIAL_CHECK) is True
    assert result2.get("data", {}).get(CONF_DISABLE_REPAIR) is True
