"""The tests for the retry integration."""
from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, patch
import voluptuous as vol

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.hassio.const import ATTR_DATA
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_DEVICE_ID,
    ATTR_SERVICE,
    CONF_CHOOSE,
    CONF_CONDITION,
    CONF_CONDITIONS,
    CONF_COUNT,
    CONF_DEFAULT,
    CONF_ELSE,
    CONF_ENTITIES,
    CONF_IF,
    CONF_NAME,
    CONF_PARALLEL,
    CONF_PLATFORM,
    CONF_REPEAT,
    CONF_SEQUENCE,
    CONF_TARGET,
    CONF_THEN,
    CONF_VALUE_TEMPLATE,
    ENTITY_MATCH_ALL,
    ENTITY_MATCH_NONE,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    IntegrationError,
    InvalidEntityFormatError,
    ServiceNotFound,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.retry.const import (
    ACTIONS_SERVICE,
    ATTR_EXPECTED_STATE,
    ATTR_INDIVIDUALLY,
    ATTR_RETRIES,
    CALL_SERVICE,
    DOMAIN,
)

TEST_SERVICE = "test_service"
BASIC_SEQUENCE_DATA = [{ATTR_SERVICE: f"{DOMAIN}.{TEST_SERVICE}"}]


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
                **cv.TARGET_SERVICE_FIELDS,
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


async def async_call(
    hass: HomeAssistant,
    data: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> None:
    """Call a service via the retry service."""
    data = data or {}
    data[ATTR_SERVICE] = f"{DOMAIN}.{TEST_SERVICE}"
    await hass.services.async_call(DOMAIN, CALL_SERVICE, data, True, target=target)


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
    await hass.async_block_till_done()
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
    await async_shutdown(hass, freezer)
    for entity in entities:
        assert f"{entity} is not available" in caplog.text


async def test_selective_retry_together(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test retry on part of entities."""
    entities = ["binary_sensor.test", "binary_sensor.invalid"]
    calls = await async_setup(hass, False)
    await async_call(
        hass,
        {
            ATTR_ENTITY_ID: entities,
            ATTR_DEVICE_ID: ENTITY_MATCH_NONE,
            ATTR_INDIVIDUALLY: False,
        },
    )
    await async_shutdown(hass, freezer)
    assert calls[0].data[ATTR_ENTITY_ID] == entities
    assert ATTR_DEVICE_ID in calls[0].data
    assert calls[1].data[ATTR_ENTITY_ID] == ["binary_sensor.invalid"]
    assert ATTR_DEVICE_ID not in calls[1].data


async def test_selective_retry_individually(
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
    called_entities = [x.data[ATTR_ENTITY_ID] for x in calls]
    assert called_entities.count(["binary_sensor.test"]) == 1
    assert called_entities.count(["binary_sensor.invalid"]) == 7
    assert ATTR_DEVICE_ID not in calls[0].data


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
    await async_shutdown(hass, freezer)
    assert 'binary_sensor.test state is "on" but expecting "off"' in caplog.text
    wait_times = [x.args[0] for x in sleep_mock.await_args_list]
    assert wait_times.count(0.2) == 7


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
    await async_shutdown(hass, freezer)
    assert f"{entity} is not available" in caplog.text


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
    await async_shutdown(hass, freezer)
    assert f"{entity} is not available" in caplog.text


async def test_template(hass: HomeAssistant) -> None:
    """Test retry_service with template."""
    calls = await async_setup(hass, False)
    await hass.services.async_call(
        DOMAIN, CALL_SERVICE, {ATTR_SERVICE: '{{ "retry.test_service" }}'}, True
    )
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_invalid_service(hass: HomeAssistant) -> None:
    """Test invalid service."""
    await async_setup(hass)
    with pytest.raises(ServiceNotFound):
        await hass.services.async_call(
            DOMAIN, CALL_SERVICE, {ATTR_SERVICE: "invalid.service"}, True
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
        await async_call(hass, target={ATTR_ENTITY_ID: ENTITY_MATCH_ALL})


async def test_unload(hass: HomeAssistant) -> None:
    """Test we abort if already setup."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, CALL_SERVICE)
    assert hass.services.has_service(DOMAIN, ACTIONS_SERVICE)

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, CALL_SERVICE)
    assert not hass.services.has_service(DOMAIN, ACTIONS_SERVICE)


async def test_configuration_yaml(hass: HomeAssistant) -> None:
    """Test initialization via configuration.yaml."""
    assert not hass.services.has_service(DOMAIN, CALL_SERVICE)
    assert not hass.services.has_service(DOMAIN, ACTIONS_SERVICE)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, CALL_SERVICE)
    assert hass.services.has_service(DOMAIN, ACTIONS_SERVICE)


@pytest.mark.parametrize(
    ["service_data"],
    [
        ({CONF_SEQUENCE: BASIC_SEQUENCE_DATA},),
        (
            {
                CONF_SEQUENCE: [
                    {
                        CONF_REPEAT: {
                            CONF_COUNT: 1,
                            CONF_SEQUENCE: BASIC_SEQUENCE_DATA,
                        },
                    },
                ],
            },
        ),
        (
            {
                CONF_SEQUENCE: [
                    {
                        CONF_CHOOSE: [
                            {
                                CONF_CONDITIONS: [
                                    {
                                        CONF_CONDITION: "template",
                                        CONF_VALUE_TEMPLATE: "{{ True }}",
                                    }
                                ],
                                CONF_SEQUENCE: BASIC_SEQUENCE_DATA,
                            }
                        ],
                    }
                ]
            },
        ),
        (
            {
                CONF_SEQUENCE: [
                    {
                        CONF_CHOOSE: [],
                        CONF_DEFAULT: BASIC_SEQUENCE_DATA,
                    }
                ]
            },
        ),
        (
            {
                CONF_SEQUENCE: [
                    {
                        CONF_IF: [
                            {
                                CONF_CONDITION: "template",
                                CONF_VALUE_TEMPLATE: "{{ True }}",
                            }
                        ],
                        CONF_THEN: BASIC_SEQUENCE_DATA,
                    }
                ],
            },
        ),
        (
            {
                CONF_SEQUENCE: [
                    {
                        CONF_IF: [
                            {
                                CONF_CONDITION: "template",
                                CONF_VALUE_TEMPLATE: "{{ False }}",
                            }
                        ],
                        CONF_THEN: [],
                        CONF_ELSE: BASIC_SEQUENCE_DATA,
                    }
                ],
            },
        ),
        (
            {
                CONF_SEQUENCE: [
                    {CONF_PARALLEL: BASIC_SEQUENCE_DATA},
                ],
            },
        ),
        (
            {
                CONF_SEQUENCE: [
                    {
                        CONF_PARALLEL: {CONF_SEQUENCE: BASIC_SEQUENCE_DATA},
                    },
                ],
            },
        ),
        (
            {
                CONF_SEQUENCE: [
                    {CONF_CONDITION: "template", CONF_VALUE_TEMPLATE: "{{True}}"},
                    {**(BASIC_SEQUENCE_DATA[0])},
                ],
            },
        ),
    ],
    ids=[
        "service call",
        "repeat",
        "choose",
        "choose-default",
        "if",
        "if-else",
        "parallel-short",
        "parallel",
        "multiple",
    ],
)
async def test_actions_service(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    service_data: dict[str, any],
) -> None:
    """Test action service."""
    calls = await async_setup(hass)
    await hass.services.async_call(
        DOMAIN,
        ACTIONS_SERVICE,
        service_data,
        True,
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 7


async def test_action_types() -> None:
    """Test that no new action type was added."""
    assert list(cv.ACTION_TYPE_SCHEMAS.keys()) == [
        cv.SCRIPT_ACTION_CALL_SERVICE,
        cv.SCRIPT_ACTION_DELAY,
        cv.SCRIPT_ACTION_WAIT_TEMPLATE,
        cv.SCRIPT_ACTION_FIRE_EVENT,
        cv.SCRIPT_ACTION_CHECK_CONDITION,
        cv.SCRIPT_ACTION_DEVICE_AUTOMATION,
        cv.SCRIPT_ACTION_ACTIVATE_SCENE,
        cv.SCRIPT_ACTION_REPEAT,
        cv.SCRIPT_ACTION_CHOOSE,
        cv.SCRIPT_ACTION_WAIT_FOR_TRIGGER,
        cv.SCRIPT_ACTION_VARIABLES,
        cv.SCRIPT_ACTION_STOP,
        cv.SCRIPT_ACTION_IF,
        cv.SCRIPT_ACTION_PARALLEL,
    ]


async def test_actions_propagating_args(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test action service propagate correctly the arguments."""
    calls = await async_setup(hass)
    await hass.services.async_call(
        DOMAIN,
        ACTIONS_SERVICE,
        {CONF_SEQUENCE: BASIC_SEQUENCE_DATA, ATTR_RETRIES: 3},
        True,
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 3


async def test_actions_inner_service_validation(
    hass: HomeAssistant,
) -> None:
    """Test action service validate retry call parameters."""
    await async_setup(hass)
    with pytest.raises(ServiceNotFound):
        await hass.services.async_call(
            DOMAIN,
            ACTIONS_SERVICE,
            {CONF_SEQUENCE: [{ATTR_SERVICE: f"{DOMAIN}.invalid"}]},
            True,
        )


async def test_nested_actions(
    hass: HomeAssistant,
) -> None:
    """Test nested actions of retry.actions."""
    await async_setup(hass)
    with pytest.raises(IntegrationError):
        await hass.services.async_call(
            DOMAIN,
            ACTIONS_SERVICE,
            {
                CONF_SEQUENCE: [
                    {
                        ATTR_SERVICE: f"{DOMAIN}.{ACTIONS_SERVICE}",
                        ATTR_DATA: {CONF_SEQUENCE: BASIC_SEQUENCE_DATA},
                    }
                ]
            },
            True,
        )


async def test_call_in_actions(
    hass: HomeAssistant,
) -> None:
    """Test retry.call inside retry.actions."""
    await async_setup(hass)
    with pytest.raises(IntegrationError):
        await hass.services.async_call(
            DOMAIN,
            ACTIONS_SERVICE,
            {
                CONF_SEQUENCE: [
                    {
                        ATTR_SERVICE: f"{DOMAIN}.{CALL_SERVICE}",
                    }
                ]
            },
            True,
        )


async def test_all_entities_actions(
    hass: HomeAssistant,
) -> None:
    """Test all entities is not allowed in retry.actions."""
    await async_setup(hass)
    with pytest.raises(InvalidEntityFormatError):
        await hass.services.async_call(
            DOMAIN,
            ACTIONS_SERVICE,
            {
                CONF_SEQUENCE: [
                    {
                        ATTR_SERVICE: f"{DOMAIN}.{TEST_SERVICE}",
                        CONF_TARGET: {ATTR_ENTITY_ID: ENTITY_MATCH_ALL},
                    }
                ]
            },
            True,
        )
