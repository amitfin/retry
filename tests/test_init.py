"""The tests for the retry integration."""
from __future__ import annotations

import datetime
from typing import Any

import pytest

from homeassistant.const import ATTR_SERVICE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.retry.const import ATTR_RETRIES, DOMAIN, SERVICE


async def async_setup(hass: HomeAssistant, raises: bool = True) -> list[ServiceCall]:
    """Load retry custom integration and basic environment."""
    assert await async_setup_component(hass, DOMAIN, {})
    assert await async_setup_component(hass, "sun", {})

    calls = []

    @callback
    def async_service(service_call: ServiceCall):
        """Mock service call."""
        calls.append(service_call)
        if raises:
            raise Exception()  # pylint: disable=broad-exception-raised

    hass.services.async_register(DOMAIN, "test", async_service)

    return calls


async def async_call(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Call a service via the retry service."""
    data[ATTR_SERVICE] = f"{DOMAIN}.test"
    await hass.services.async_call(DOMAIN, SERVICE, data, True)


async def test_success(hass: HomeAssistant, freezer) -> None:
    """Test success case."""
    now = datetime.datetime.fromisoformat("2000-01-01")
    freezer.move_to(now)
    calls = await async_setup(hass, False)
    await async_call(hass, {"entity_id": ["sun.sun", "sun.sun"]})
    now += datetime.timedelta(hours=1)
    freezer.move_to(now)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls) == 1


@pytest.mark.parametrize(
    ["retries"],
    [(6,), (3,), (10,)],
    ids=["default", "3-retries", "10-retries"],
)
async def test_failure(hass: HomeAssistant, freezer, retries) -> None:
    """Test failed service calls."""
    now = datetime.datetime.fromisoformat("2000-01-01")
    freezer.move_to(now)
    calls = await async_setup(hass)
    data = {}
    if retries != 6:
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


async def test_entity_unavaliable(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test entity is not avaliable."""
    entity = "sun.moon"
    await async_setup(hass, False)
    await async_call(hass, {"entity_id": entity})
    assert f"{entity} is not avaliable" in caplog.text


async def test_template(
    hass: HomeAssistant,
) -> None:
    """Test retry_service with template."""
    calls = await async_setup(hass, False)
    await hass.services.async_call(
        DOMAIN, SERVICE, {ATTR_SERVICE: '{{ "retry.test" }}'}, True
    )
    assert len(calls) == 1
