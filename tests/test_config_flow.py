"""Tests for the retry config flow."""
from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.retry.const import DOMAIN


async def test_simple(hass: HomeAssistant) -> None:
    """Test a simple user flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=[]
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
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
