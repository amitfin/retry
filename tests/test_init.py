"""The tests for the retry integration."""
from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, patch
import voluptuous as vol

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_DEVICE_ID,
    ATTR_SERVICE,
    CONF_ENTITIES,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_TARGET,
    ENTITY_MATCH_ALL,
    ENTITY_MATCH_NONE,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import InvalidEntityFormatError, ServiceNotFound
from homeassistant.helpers import config_validation as cv
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.retry.const import (
    ATTR_EXPECTED_STATE,
    ATTR_RETRIES,
    DOMAIN,
    SERVICE,
)

TEST_SERVICE = "test_service"


async def async_setup(hass: HomeAssistant, raises: bool = True) -> list[ServiceCall]:
    """Load retry custom integration and basic environment."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert await async_setup_component(
        hass,
        "template",
        {"template": {"binary_sensor": [{"name": "test", "state": "{{ True }}"}]}},
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
                vol.Optional(ATTR_ENTITY_ID): vol.Any(cv.entity_ids, ENTITY_MATCH_ALL),
                vol.Optional(ATTR_DEVICE_ID): cv.string,
                vol.Optional(CONF_TARGET): cv.TARGET_SERVICE_FIELDS,
            },
        ),
    )

    return calls


async def async_next_hour(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Jump to the next hour and execute all pending timers."""
    freezer.move_to(dt_util.now() + datetime.timedelta(hours=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def async_shutdown(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Make sure all pending retries were executed."""
    for _ in range(10):
        await async_next_hour(hass, freezer)


async def async_call(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Call a service via the retry service."""
    data[ATTR_SERVICE] = f"{DOMAIN}.{TEST_SERVICE}"
    assert await hass.services.async_call(DOMAIN, SERVICE, data, True)


async def test_success(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Test success case."""
    calls = await async_setup(hass, False)
    await async_call(
        hass, {ATTR_ENTITY_ID: ["binary_sensor.test", "binary_sensor.test"]}
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ["retries"],
    [(7,), (3,), (10,)],
    ids=["default", "3-retries", "10-retries"],
)
async def test_failure(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, retries: int
) -> None:
    """Test failed service calls."""
    calls = await async_setup(hass)
    data = {}
    if retries != 7:
        data[ATTR_RETRIES] = retries
    await async_call(hass, data)
    for i in range(20):
        if i < retries:
            assert len(calls) == (i + 1)
        await async_next_hour(hass, freezer)
    assert len(calls) == retries


async def test_entity_unavailable(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test entities are not available."""
    entities = ["binary_sensor.invalid1", "binary_sensor.invalid2"]
    await async_setup(hass, False)
    await async_call(hass, {ATTR_ENTITY_ID: entities, ATTR_EXPECTED_STATE: "on"})
    for entity in entities:
        assert f"{entity} is not available" in caplog.text
    await async_shutdown(hass, freezer)


async def test_selective_retry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test retry on part of entities."""
    entities = ["binary_sensor.test", "binary_sensor.invalid"]
    calls = await async_setup(hass, False)
    await async_call(
        hass, {ATTR_ENTITY_ID: entities, ATTR_DEVICE_ID: ENTITY_MATCH_NONE}
    )
    await async_shutdown(hass, freezer)
    assert calls[0].data[ATTR_ENTITY_ID] == entities
    assert ATTR_DEVICE_ID in calls[0].data
    assert calls[1].data[ATTR_ENTITY_ID] == ["binary_sensor.invalid"]
    assert ATTR_DEVICE_ID not in calls[1].data


@patch("custom_components.retry.asyncio.sleep")
async def test_entity_wrong_state(
    sleep_mock: AsyncMock,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test entity has the wrong state."""
    await async_setup(hass, False)
    await async_call(
        hass,
        {
            ATTR_ENTITY_ID: "binary_sensor.test",
            ATTR_EXPECTED_STATE: "{{ 'off' }}",
        },
    )
    assert 'binary_sensor.test state is "on" but expecting "off"' in caplog.text
    assert sleep_mock.await_args.args[0] == 0.2
    await async_shutdown(hass, freezer)


async def test_group_entity_unavailable(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test entity is not available."""
    entity = "light.invalid"
    await async_setup(hass, False)
    assert await async_setup_component(
        hass, "group", {"group": {"test": {CONF_ENTITIES: [entity]}}}
    )
    await hass.async_block_till_done()
    await async_call(hass, {ATTR_ENTITY_ID: "group.test"})
    assert f"{entity} is not available" in caplog.text
    await async_shutdown(hass, freezer)


async def test_group_platform_entity_unavailable(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test entity is not available."""
    entity = "light.invalid"
    await async_setup(hass, False)
    assert await async_setup_component(
        hass,
        "light",
        {
            "light": [
                {CONF_NAME: "test", CONF_PLATFORM: "group", CONF_ENTITIES: [entity]}
            ]
        },
    )
    await hass.async_block_till_done()
    await async_call(hass, {ATTR_ENTITY_ID: "light.test"})
    assert f"{entity} is not available" in caplog.text
    await async_shutdown(hass, freezer)


async def test_template(hass: HomeAssistant) -> None:
    """Test retry_service with template."""
    calls = await async_setup(hass, False)
    await hass.services.async_call(
        DOMAIN, SERVICE, {ATTR_SERVICE: '{{ "retry.test_service" }}'}, True
    )
    assert len(calls) == 1


async def test_invalid_service(hass: HomeAssistant) -> None:
    """Test invalid service."""
    await async_setup(hass)
    with pytest.raises(ServiceNotFound):
        await hass.services.async_call(
            DOMAIN, SERVICE, {ATTR_SERVICE: "invalid.service"}, True
        )


async def test_invalid_schema(hass: HomeAssistant) -> None:
    """Test invalid schema."""
    await async_setup(hass)
    with pytest.raises(vol.Invalid):
        await async_call(hass, {"invalid_field": ""})


async def test_all_entities(hass: HomeAssistant) -> None:
    """Test selecting all entities."""
    await async_setup(hass)
    with pytest.raises(InvalidEntityFormatError):
        await async_call(hass, {ATTR_ENTITY_ID: ENTITY_MATCH_ALL})


async def test_all_entities_in_target(hass: HomeAssistant) -> None:
    """Test selecting all entities in the target key."""
    await async_setup(hass)
    with pytest.raises(InvalidEntityFormatError):
        await async_call(hass, {CONF_TARGET: {ATTR_ENTITY_ID: ENTITY_MATCH_ALL}})


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


async def test_configuration_yaml(hass: HomeAssistant) -> None:
    """Test initialization via configuration.yaml."""
    assert not hass.services.has_service(DOMAIN, SERVICE)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, SERVICE)
