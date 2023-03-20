"""The tests for the retry integration."""
from __future__ import annotations

import datetime
from typing import Any
import voluptuous as vol

import pytest

from homeassistant.const import ATTR_ENTITY_ID, ATTR_SERVICE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import config_validation as cv
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.retry.const import ATTR_RETRIES, DOMAIN, SERVICE

TEST_SERVICE = "test_service"


async def async_setup(hass: HomeAssistant, raises: bool = True) -> list[ServiceCall]:
    """Load retry custom integration and basic environment."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert await async_setup_component(
        hass, "script", {"script": {"test": {"sequence": []}}}
    )
    await hass.async_block_till_done()

    calls = []

    @callback
    def async_service(service_call: ServiceCall):
        """Mock service call."""
        calls.append(service_call)
        if raises:
            raise Exception()  # pylint: disable=broad-exception-raised

    hass.services.async_register(
        DOMAIN,
        TEST_SERVICE,
        async_service,
        vol.Schema(
            {
                vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
            }
        ),
    )

    return calls


async def async_call(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Call a service via the retry service."""
    data[ATTR_SERVICE] = f"{DOMAIN}.{TEST_SERVICE}"
    await hass.services.async_call(DOMAIN, SERVICE, data, True)


async def test_success(hass: HomeAssistant, freezer) -> None:
    """Test success case."""
    now = datetime.datetime.fromisoformat("2000-01-01")
    freezer.move_to(now)
    calls = await async_setup(hass, False)
    await async_call(hass, {ATTR_ENTITY_ID: ["script.test", "script.test"]})
    now += datetime.timedelta(hours=1)
    freezer.move_to(now)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls) == 1


@pytest.mark.parametrize(
    ["retries"],
    [(7,), (3,), (10,)],
    ids=["default", "3-retries", "10-retries"],
)
async def test_failure(hass: HomeAssistant, freezer, retries) -> None:
    """Test failed service calls."""
    now = datetime.datetime.fromisoformat("2000-01-01")
    freezer.move_to(now)
    calls = await async_setup(hass)
    data = {}
    if retries != 7:
        data[ATTR_RETRIES] = retries
    await async_call(hass, data)
    for i in range(20):
        if i < retries:
            assert len(calls) == (i + 1)
        now += datetime.timedelta(hours=1)
        freezer.move_to(now)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert len(calls) == retries


async def test_entity_unavailable(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test entity is not avaliable."""
    entity = "script.invalid"
    await async_setup(hass, False)
    await async_call(hass, {ATTR_ENTITY_ID: entity})
    assert f"{entity} is not available" in caplog.text


async def test_template(
    hass: HomeAssistant,
) -> None:
    """Test retry_service with template."""
    calls = await async_setup(hass, False)
    await hass.services.async_call(
        DOMAIN, SERVICE, {ATTR_SERVICE: '{{ "retry.test_service" }}'}, True
    )
    assert len(calls) == 1


async def test_invalid_service(
    hass: HomeAssistant,
) -> None:
    """Test invalid service."""
    await async_setup(hass)
    with pytest.raises(ServiceNotFound):
        await hass.services.async_call(
            DOMAIN, SERVICE, {ATTR_SERVICE: "invalid.service"}, True
        )


async def test_invalid_schema(
    hass: HomeAssistant,
) -> None:
    """Test invalid schema."""
    await async_setup(hass)
    with pytest.raises(vol.Invalid):
        await async_call(hass, {"invalid_field": ""})


async def test_unload(hass: HomeAssistant) -> None:
    """Test we abort if already setup."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, SERVICE)

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, SERVICE)
