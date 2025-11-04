"""Tests for the diagnostics data."""

from http import HTTPStatus

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.retry.const import DOMAIN


@pytest.mark.parametrize(
    "allowed_logs",
    [["zlib_ng and isal are not available"]],
    indirect=True,
)
async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test diagnostics."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert await async_setup_component(hass, "diagnostics", {})
    await hass.async_block_till_done()

    client = await hass_client()
    diagnostics = await client.get(
        f"/api/diagnostics/config_entry/{config_entry.entry_id}"
    )
    assert diagnostics.status == HTTPStatus.OK
    assert (await diagnostics.json())["data"] == {}

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
