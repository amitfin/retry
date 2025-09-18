"""The tests for the retry integration."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import homeassistant.util.dt as dt_util
import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import (
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_ACTION,
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
    CONF_SERVICE_DATA,
    CONF_TARGET,
    CONF_THEN,
    CONF_VALUE_TEMPLATE,
    ENTITY_MATCH_ALL,
    ENTITY_MATCH_NONE,
    EVENT_CALL_SERVICE,
)
from homeassistant.core import Context, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    IntegrationError,
    ServiceNotFound,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import script
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.retry.const import (
    ACTION_SERVICE,
    ACTIONS_SERVICE,
    ATTR_BACKOFF,
    ATTR_EXPECTED_STATE,
    ATTR_IGNORE_TARGET,
    ATTR_ON_ERROR,
    ATTR_REPAIR,
    ATTR_RETRIES,
    ATTR_RETRY_ID,
    ATTR_STATE_DELAY,
    ATTR_STATE_GRACE,
    ATTR_VALIDATION,
    CONF_DISABLE_INITIAL_CHECK,
    CONF_DISABLE_REPAIR,
    DOMAIN,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from freezegun.api import FrozenDateTimeFactory

TEST_SERVICE = "test_service"
TEST_ON_ERROR_SERVICE = "test_on_error_service"
BASIC_SEQUENCE_DATA = [{CONF_ACTION: f"{DOMAIN}.{TEST_SERVICE}"}]


@pytest.fixture(autouse=True)
def nothing_deprecated(caplog: pytest.LogCaptureFixture) -> Generator[None]:
    """Ensure no deprecation warnings are logged."""
    yield
    for record in caplog.get_records(when="call"):
        message = record.getMessage()
        assert "deprecated" not in message


async def async_setup(
    hass: HomeAssistant,
    raises: bool = True,  # noqa: FBT001, FBT002
    options: dict | None = None,
) -> list[ServiceCall]:
    """Load retry custom integration and basic environment."""
    config_entry = MockConfigEntry(domain=DOMAIN, options=options)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert await async_setup_component(
        hass,
        "template",
        {
            "template": {
                "binary_sensor": [
                    {"name": "test", "state": "{{ True }}"},
                    {"name": "test2", "state": "{{ False }}"},
                ]
            }
        },
    )
    await hass.async_block_till_done()

    calls = []

    @callback
    def async_service(service_call: ServiceCall) -> None:
        """Mock service call."""
        calls.append(service_call)
        if service_call.service == TEST_SERVICE and raises:
            raise Exception  # noqa: TRY002

    hass.services.async_register(
        DOMAIN,
        TEST_SERVICE,
        async_service,
        vol.Schema(
            {
                **cv.TARGET_SERVICE_FIELDS,
                vol.Optional("test"): vol.Any(str, [str]),
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        TEST_ON_ERROR_SERVICE,
        async_service,
    )

    return calls


async def async_next_seconds(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Jump to the next "seconds" and execute all pending timers."""
    freezer.move_to(dt_util.now() + datetime.timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def async_shutdown(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Make sure all pending retries were performed."""
    for _ in range(10):
        await async_next_seconds(hass, freezer, 3600)


async def async_call(
    hass: HomeAssistant,
    data: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> None:
    """Call a service via the retry service."""
    data = data or {}
    data[CONF_ACTION] = f"{DOMAIN}.{TEST_SERVICE}"
    await hass.services.async_call(
        DOMAIN, ACTION_SERVICE, data, blocking=True, target=target
    )


async def test_success(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Test success case."""
    calls = await async_setup(hass, raises=False)
    await async_call(
        hass, {ATTR_ENTITY_ID: ["binary_sensor.test", "binary_sensor.test"]}
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "retries",
    [7, 3, 10],
    ids=["default", "3-retries", "10-retries"],
)
async def test_failure(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    retries: int,
) -> None:
    """Test failed actions."""
    repairs = async_capture_events(hass, str(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED))
    calls = await async_setup(hass)
    data = {}
    if retries != 7:
        data[ATTR_RETRIES] = retries
    await async_call(hass, data)
    await hass.async_block_till_done()
    for i in range(20):
        if i < retries:
            assert len(calls) == (i + 1)
        await async_next_seconds(hass, freezer, 3600)
    assert len(calls) == retries
    assert (
        f"[Failed]: attempt {retries}/{retries}: {DOMAIN}.{TEST_SERVICE}()"
        in caplog.text
    )
    assert len(repairs) == 1
    assert repairs[0].data["action"] == "create"
    assert repairs[0].data["domain"] == DOMAIN
    assert repairs[0].data["issue_id"] == f"{DOMAIN}.{TEST_SERVICE}()"


async def test_entity_unavailable(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test entities are not available."""
    entities = ["binary_sensor.invalid1", "binary_sensor.invalid2"]
    await async_setup(hass, raises=False)
    await async_call(hass, {ATTR_ENTITY_ID: entities, ATTR_EXPECTED_STATE: "on"})
    await async_shutdown(hass, freezer)
    for entity in entities:
        assert f"{entity} is not available" in caplog.text
        assert (
            f"[Failed]: attempt 7/7: {DOMAIN}.{TEST_SERVICE}(entity_id={entity})"
            "[expected_state=on]"
        ) in caplog.text


async def test_selective_retry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test retry on part of entities."""
    entities = ["binary_sensor.test", "binary_sensor.invalid"]
    calls = await async_setup(hass, raises=False)
    await async_call(
        hass, {ATTR_ENTITY_ID: entities, ATTR_DEVICE_ID: ENTITY_MATCH_NONE}
    )
    await async_shutdown(hass, freezer)
    called_entities = [x.data[ATTR_ENTITY_ID] for x in calls]
    assert called_entities.count(["binary_sensor.test"]) == 1
    assert called_entities.count(["binary_sensor.invalid"]) == 7
    assert ATTR_DEVICE_ID not in calls[0].data


async def test_ignore_target(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test ignore_target parameter."""
    entities = ["binary_sensor.test", "binary_sensor.invalid"]
    calls = await async_setup(hass)
    await async_call(
        hass,
        {
            ATTR_ENTITY_ID: entities,
            ATTR_DEVICE_ID: ENTITY_MATCH_NONE,
            ATTR_IGNORE_TARGET: True,
        },
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 7
    assert calls[0].data[ATTR_ENTITY_ID] == entities
    assert calls[0].data[ATTR_DEVICE_ID] == ENTITY_MATCH_NONE
    assert (
        "[Failed]: attempt 7/7: retry.test_service("
        "entity_id=['binary_sensor.test', 'binary_sensor.invalid'], device_id=none)"
        "[ignore_target=True]"
    ) in caplog.text


@pytest.mark.parametrize(
    ("expected_state", "validation", "grace"),
    [
        ("{{ 'off' }}", None, None),
        ("{{ 'off' }}", None, 3.21),
        (None, "[[ is_state(entity_id, 'off') ]]", 1.23),
    ],
    ids=["default", "grace", "validation"],
)
async def test_entity_wrong_state(  # noqa: PLR0913
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    sleep: AsyncMock,
    expected_state: str | None,
    validation: str | None,
    grace: float | None,
) -> None:
    """Test entity has the wrong state."""
    await async_setup(hass, raises=False)
    await async_call(
        hass,
        {
            ATTR_ENTITY_ID: "binary_sensor.test",
            **({ATTR_EXPECTED_STATE: expected_state} if expected_state else {}),
            **({ATTR_VALIDATION: validation} if validation else {}),
            **({ATTR_STATE_GRACE: grace} if grace else {}),
        },
    )
    await async_shutdown(hass, freezer)
    if expected_state:
        assert (
            'binary_sensor.test state is "on" but expecting one of "[\'off\']"'
            in caplog.text
        )
    if validation:
        validation = validation.replace("[", "{").replace("]", "}")
        assert f'"{validation}" is False' in caplog.text
    wait_times = [x.args[0] for x in sleep.await_args_list]
    assert wait_times.count(grace or 0.2) == 7


async def test_state_delay(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    sleep: AsyncMock,
) -> None:
    """Test initial state delay time."""
    await async_setup(hass, raises=False)
    await async_call(
        hass,
        {
            ATTR_ENTITY_ID: "binary_sensor.test",
            ATTR_EXPECTED_STATE: "off",
            ATTR_STATE_DELAY: 1.2,
        },
    )
    await async_shutdown(hass, freezer)
    wait_times = [x.args[0] for x in sleep.await_args_list]
    assert wait_times.count(1.2) == 7  # state_delay
    assert wait_times.count(0.2) == 7  # state_grace
    assert (
        f"[Failed]: attempt 7/7: {DOMAIN}.{TEST_SERVICE}(entity_id=binary_sensor.test)"
        "[expected_state=off, state_delay=1.2]"
    ) in caplog.text


@pytest.mark.parametrize(
    ("options", "call_count"),
    [
        ({}, 0),
        ({CONF_DISABLE_INITIAL_CHECK: True}, 1),
    ],
    ids=["initial check", "without initial check"],
)
async def test_entity_expected_state_list(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    options: dict[str, Any],
    call_count: int,
) -> None:
    """Test list of expected states."""
    calls = await async_setup(hass, raises=False, options=options)
    await async_call(
        hass,
        {
            ATTR_ENTITY_ID: "binary_sensor.test",
            ATTR_EXPECTED_STATE: ["dummy", "{{ 'on' }}"],
        },
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == call_count


async def test_validation_success(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test successful validation."""
    calls = await async_setup(hass, raises=False)
    await async_call(
        hass,
        {
            ATTR_ENTITY_ID: "binary_sensor.test",
            "test": ["test"],
            ATTR_VALIDATION: (
                "[# Test #][% set x = entity_id %]"
                "[[ states(x) in ['on'] and action == 'retry.test_service'"
                " and test == ['test'] ]]"
            ),
        },
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 0


async def test_float_point_zero(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test validation of a float with point zero."""
    calls = await async_setup(hass, raises=False)
    await async_setup_component(
        hass,
        "input_number",
        {
            "input_number": {
                "test": {"min": 0, "max": 100, "initial": 50},
            }
        },
    )
    await async_call(
        hass,
        {
            ATTR_ENTITY_ID: "input_number.test",
            ATTR_EXPECTED_STATE: "50",
        },
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 0


@pytest.mark.parametrize(
    "count",
    [0, 1, 3, 5],
    ids=["0", "1", "3", "5"],
)
async def test_validation_with_attempt(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    count: int,
) -> None:
    """Test validation as a condition of attempt variable."""
    calls = await async_setup(hass, raises=False)
    await async_call(
        hass,
        {
            ATTR_VALIDATION: f"[[ attempt >= {count} ]]",
        },
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == count


async def test_retry_id_cancellation(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test parallel reties cancellation logic."""
    calls = await async_setup(hass)
    for _ in range(2):
        await async_call(hass)
    await async_shutdown(hass, freezer)
    assert len(calls) == 8  # = 1 + 7
    assert "[Cancelled]: attempt 2/7: retry.test_service()" in caplog.text


async def test_retry_id_sequence(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test reties with the same ID running one after the other."""
    calls = await async_setup(hass)
    for _ in range(2):
        await async_call(hass)
        await async_shutdown(hass, freezer)
    assert len(calls) == 14  # = 7 + 7


async def test_retry_id_success_sequence(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test successful reties with the same ID running one after the other."""
    calls = await async_setup(hass, raises=False)
    for _ in range(2):
        await async_call(hass)
        await async_shutdown(hass, freezer)
    assert len(calls) == 2  # = 1 + 1


async def test_different_retry_ids(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test parallel reties with different IDs."""
    calls = await async_setup(hass)
    for i in range(2):
        await async_call(hass, {ATTR_RETRY_ID: str(i)})
    await async_shutdown(hass, freezer)
    assert len(calls) == 14  # = 7 + 7
    for i in range(2):
        assert f'{DOMAIN}.{TEST_SERVICE}()[{ATTR_RETRY_ID}="{i}"]' in caplog.text


async def test_default_retry_id_is_entity_id(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test parallel reties with different IDs."""
    calls = await async_setup(hass)
    await async_call(hass, {ATTR_ENTITY_ID: "binary_sensor.test"})
    await async_call(hass, {ATTR_RETRY_ID: "binary_sensor.test"})
    await async_shutdown(hass, freezer)
    assert len(calls) == 8  # = 1 + 7


async def test_default_retry_id_is_domain_service(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test parallel reties with different IDs."""
    calls = await async_setup(hass)
    await async_call(hass)
    await async_call(hass, {ATTR_RETRY_ID: f"{DOMAIN}.{TEST_SERVICE}"})
    await async_shutdown(hass, freezer)
    assert len(calls) == 8  # = 1 + 7


@pytest.mark.parametrize(
    "value",
    [None, ""],
    ids=["none", "empty"],
)
async def test_disable_retry_id(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    value: str | None,
) -> None:
    """Test disabling retry_id."""
    calls = await async_setup(hass)
    for _ in range(2):
        await async_call(hass, {ATTR_RETRY_ID: value})
    await async_shutdown(hass, freezer)
    assert len(calls) == 14  # = 7 + 7
    if value is not None:
        value = f'"{value}"'
    assert f"{DOMAIN}.{TEST_SERVICE}()[{ATTR_RETRY_ID}={value}]" in caplog.text


async def test_multi_entities_retry_id(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test retry_id with multiple entities."""
    calls = await async_setup(hass)
    await async_call(
        hass,
        {
            ATTR_ENTITY_ID: ["binary_sensor.test", "binary_sensor.test2"],
            ATTR_RETRY_ID: "id",
        },
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 14  # = 7 + 7


async def test_on_error(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test on_error parameter."""
    calls = await async_setup(hass)
    await async_call(
        hass,
        {
            ATTR_ENTITY_ID: "binary_sensor.test",
            ATTR_ON_ERROR: [
                {
                    CONF_ACTION: f"{DOMAIN}.{TEST_ON_ERROR_SERVICE}",
                    CONF_SERVICE_DATA: {
                        ATTR_ENTITY_ID: "{{ entity_id }}",
                        "test": "{{ action }}",
                    },
                }
            ],
        },
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 8
    assert calls[-1].service == TEST_ON_ERROR_SERVICE
    assert calls[-1].data[ATTR_ENTITY_ID] == "binary_sensor.test"
    assert calls[-1].data["test"] == f"{DOMAIN}.{TEST_SERVICE}"


async def test_validation_in_automation(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test validation in an automation rule."""
    calls = []
    await async_setup(hass)

    @callback
    def async_service(service_call: ServiceCall) -> None:
        """Mock service call."""
        calls.append(service_call)
        freezer.tick(datetime.timedelta(seconds=1))

    hass.services.async_register(
        DOMAIN,
        "tick",
        async_service,
    )

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": "test",
                    "trigger": [],
                    "action": [
                        {
                            CONF_ACTION: f"{DOMAIN}.{ACTION_SERVICE}",
                            "data": {
                                CONF_ACTION: f"{DOMAIN}.tick",
                                ATTR_VALIDATION: (
                                    "[[ now().timestamp() == "
                                    f"{dt_util.now().timestamp() - 1} ]]"
                                ),
                            },
                        }
                    ],
                }
            ]
        },
    )
    await hass.async_block_till_done()
    await hass.services.async_call(
        "automation", "trigger", {ATTR_ENTITY_ID: "automation.test"}, blocking=True
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 7


async def test_group_entity_unavailable(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test entity is not available."""
    entity = "light.invalid"
    await async_setup(hass, raises=False)
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
    await async_setup(hass, raises=False)
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
    calls = await async_setup(hass, raises=False)
    await hass.services.async_call(
        DOMAIN,
        ACTION_SERVICE,
        {CONF_ACTION: '{{ "retry.test_service" }}'},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_invalid_service(hass: HomeAssistant) -> None:
    """Test invalid service."""
    await async_setup(hass)
    with pytest.raises(ServiceNotFound):
        await hass.services.async_call(
            DOMAIN, ACTION_SERVICE, {CONF_ACTION: "invalid.service"}, blocking=True
        )


async def test_invalid_inner_schema(hass: HomeAssistant) -> None:
    """Test invalid schema."""
    await async_setup(hass)
    with pytest.raises(vol.Invalid):
        await async_call(hass, {"invalid_field": ""})


async def test_invalid_validation(hass: HomeAssistant) -> None:
    """Test invalid validation."""
    await async_setup(hass)
    with pytest.raises(vol.Invalid):
        await async_call(hass, {ATTR_VALIDATION: "static"})


async def test_state_with_ignore_target(hass: HomeAssistant) -> None:
    """Test providing expected state with ignore_target option."""
    await async_setup(hass)

    with pytest.raises(vol.Invalid) as error:
        await async_call(hass, {ATTR_EXPECTED_STATE: "test", ATTR_IGNORE_TARGET: True})
    assert (
        str(error.value) == "must contain at most one of expected_state, ignore_target."
    )

    with pytest.raises(vol.Invalid) as error:
        await hass.services.async_call(
            DOMAIN,
            ACTIONS_SERVICE,
            {
                CONF_SEQUENCE: BASIC_SEQUENCE_DATA,
                ATTR_EXPECTED_STATE: "test",
                ATTR_IGNORE_TARGET: True,
            },
            blocking=True,
        )
    assert (
        str(error.value) == "must contain at most one of expected_state, ignore_target."
    )


async def test_state_no_entity(hass: HomeAssistant) -> None:
    """Test providing expected state without an entity."""
    await async_setup(hass)

    with pytest.raises(IntegrationError) as error:
        await async_call(hass, {ATTR_EXPECTED_STATE: "test"})
    assert str(error.value) == "expected_state parameter requires an entity"

    with pytest.raises(IntegrationError) as error:
        await hass.services.async_call(
            DOMAIN,
            ACTIONS_SERVICE,
            {CONF_SEQUENCE: BASIC_SEQUENCE_DATA, ATTR_EXPECTED_STATE: "test"},
            blocking=True,
        )
    assert str(error.value) == "expected_state parameter requires an entity"


@pytest.mark.parametrize(
    ("service", "param", "target"),
    [
        (
            ACTION_SERVICE,
            {
                CONF_ACTION: "script.turn_off",
                ATTR_EXPECTED_STATE: "on",
                ATTR_ENTITY_ID: ENTITY_MATCH_ALL,
            },
            None,
        ),
        (
            ACTION_SERVICE,
            {
                CONF_ACTION: "script.turn_off",
                ATTR_EXPECTED_STATE: "on",
            },
            {ATTR_ENTITY_ID: ENTITY_MATCH_ALL},
        ),
        (
            ACTIONS_SERVICE,
            {
                CONF_SEQUENCE: [
                    {
                        CONF_ACTION: "script.turn_off",
                        CONF_TARGET: {ATTR_ENTITY_ID: ENTITY_MATCH_ALL},
                    }
                ],
                ATTR_EXPECTED_STATE: "on",
            },
            None,
        ),
    ],
    ids=["action-param", "action-target", "actions"],
)
async def test_all_entities(  # noqa: PLR0913
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    service: str,
    param: dict,
    target: dict | None,
) -> None:
    """Test selecting all entities."""
    await async_setup(hass)
    assert await async_setup_component(
        hass,
        "script",
        {"script": {"test1": {"sequence": {}}, "test2": {"sequence": {}}}},
    )
    await hass.services.async_call(
        DOMAIN,
        service,
        param,
        blocking=True,
        target=target,
    )
    await async_shutdown(hass, freezer)
    for i in [1, 2]:
        assert (
            f"[Failed]: attempt 7/7: script.turn_off(entity_id=script.test{i})"
            "[expected_state=on]"
        ) in caplog.text


async def test_disable_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test disabling repair tickets."""
    repairs = async_capture_events(hass, str(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED))
    await async_setup(hass, options={CONF_DISABLE_REPAIR: True})
    await async_call(hass)
    await async_shutdown(hass, freezer)
    assert not len(repairs)


async def test_disable_specific_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test disabling specific repair ticket."""
    repairs = async_capture_events(hass, str(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED))
    await async_setup(hass)
    await async_call(hass, data={ATTR_REPAIR: False})
    await async_shutdown(hass, freezer)
    assert not len(repairs)


async def test_enable_specific_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test enabling specific repair ticket."""
    repairs = async_capture_events(hass, str(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED))
    await async_setup(hass, options={CONF_DISABLE_REPAIR: True})
    await async_call(hass, data={ATTR_REPAIR: True})
    await async_shutdown(hass, freezer)
    assert len(repairs)


async def test_identical_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test de-dup of identical repair tickets."""
    repairs = async_capture_events(hass, str(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED))
    await async_setup(hass)
    for _ in range(2):
        await async_call(hass)
        await async_shutdown(hass, freezer)
    assert [repair.data["action"] for repair in repairs] == [
        "create",
        "remove",
        "create",
    ]
    assert len(ir.async_get(hass).issues) == 1


async def test_action_without_config_entry(hass: HomeAssistant) -> None:
    """Test action service without config entry."""
    assert not hass.services.has_service(DOMAIN, ACTION_SERVICE)
    assert not hass.services.has_service(DOMAIN, ACTIONS_SERVICE)
    with pytest.raises(ServiceNotFound):
        await hass.services.async_call(
            DOMAIN,
            ACTION_SERVICE,
            {CONF_ACTION: f"{DOMAIN}.{TEST_SERVICE}"},
            blocking=True,
        )

    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)

    async def async_test(service_call: ServiceCall) -> None:
        pass

    hass.services.async_register(DOMAIN, TEST_SERVICE, async_test)

    await hass.services.async_call(
        DOMAIN,
        ACTION_SERVICE,
        {CONF_ACTION: f"{DOMAIN}.{TEST_SERVICE}"},
        blocking=True,
    )

    assert await hass.config_entries.async_set_disabled_by(
        config_entry.entry_id, ConfigEntryDisabler.USER
    )

    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN,
            ACTION_SERVICE,
            {CONF_ACTION: f"{DOMAIN}.{TEST_SERVICE}"},
            blocking=True,
        )
    assert str(exc.value) == "Config entry not loaded"

    assert await hass.config_entries.async_remove(config_entry.entry_id)

    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN,
            ACTION_SERVICE,
            {CONF_ACTION: f"{DOMAIN}.{TEST_SERVICE}"},
            blocking=True,
        )
    assert str(exc.value) == "Config entry not found"


async def test_configuration_yaml(hass: HomeAssistant) -> None:
    """Test initialization via configuration.yaml."""
    assert not hass.services.has_service(DOMAIN, ACTION_SERVICE)
    assert not hass.services.has_service(DOMAIN, ACTIONS_SERVICE)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, ACTION_SERVICE)
    assert hass.services.has_service(DOMAIN, ACTIONS_SERVICE)


@pytest.mark.parametrize(
    "service_data",
    [
        {CONF_SEQUENCE: BASIC_SEQUENCE_DATA},
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
        {
            CONF_SEQUENCE: [
                {
                    CONF_CHOOSE: [],
                    CONF_DEFAULT: BASIC_SEQUENCE_DATA,
                }
            ]
        },
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
        {
            CONF_SEQUENCE: [
                {CONF_PARALLEL: BASIC_SEQUENCE_DATA},
            ],
        },
        {
            CONF_SEQUENCE: [
                {
                    CONF_PARALLEL: {CONF_SEQUENCE: BASIC_SEQUENCE_DATA},
                },
            ],
        },
        {CONF_SEQUENCE: [{CONF_SEQUENCE: BASIC_SEQUENCE_DATA}]},
        {
            CONF_SEQUENCE: [
                {CONF_CONDITION: "template", CONF_VALUE_TEMPLATE: "{{True}}"},
                {**(BASIC_SEQUENCE_DATA[0])},
            ],
        },
    ],
    ids=[
        "action",
        "repeat",
        "choose",
        "choose-default",
        "if",
        "if-else",
        "parallel-short",
        "parallel",
        "sequence",
        "multiple",
    ],
)
async def test_actions_service(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    service_data: dict[str, Any],
) -> None:
    """Test action service."""
    calls = await async_setup(hass)
    await hass.services.async_call(
        DOMAIN,
        ACTIONS_SERVICE,
        service_data,
        blocking=True,
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 7


async def test_action_types() -> None:
    """Test that no new action type was added."""
    assert list(cv.ACTION_TYPE_SCHEMAS.keys()) == [
        cv.SCRIPT_ACTION_ACTIVATE_SCENE,
        cv.SCRIPT_ACTION_CALL_SERVICE,
        cv.SCRIPT_ACTION_CHECK_CONDITION,
        cv.SCRIPT_ACTION_CHOOSE,
        cv.SCRIPT_ACTION_DELAY,
        cv.SCRIPT_ACTION_DEVICE_AUTOMATION,
        cv.SCRIPT_ACTION_FIRE_EVENT,
        cv.SCRIPT_ACTION_IF,
        cv.SCRIPT_ACTION_PARALLEL,
        cv.SCRIPT_ACTION_REPEAT,
        cv.SCRIPT_ACTION_SEQUENCE,
        cv.SCRIPT_ACTION_SET_CONVERSATION_RESPONSE,
        cv.SCRIPT_ACTION_STOP,
        cv.SCRIPT_ACTION_VARIABLES,
        cv.SCRIPT_ACTION_WAIT_FOR_TRIGGER,
        cv.SCRIPT_ACTION_WAIT_TEMPLATE,
    ]


async def test_actions_propagating_args(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test action service propagate correctly the arguments."""
    calls = await async_setup(hass, raises=False)
    await hass.services.async_call(
        DOMAIN,
        ACTIONS_SERVICE,
        {
            CONF_SEQUENCE: BASIC_SEQUENCE_DATA,
            ATTR_RETRIES: 3,
            ATTR_VALIDATION: "[[ False ]]",
            ATTR_ON_ERROR: [{CONF_ACTION: f"{DOMAIN}.{TEST_ON_ERROR_SERVICE}"}],
        },
        blocking=True,
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 4
    assert (
        f"[Failed]: attempt 3/3: {DOMAIN}.{TEST_SERVICE}()"
        '[validation="{{ False }}"]'
    ) in caplog.text


@pytest.mark.parametrize(
    ("backoff", "backoff_fixed", "delays"),
    [
        (None, None, [1, 2, 4, 8, 16, 32]),
        ("10", "10", [10] * 6),
        (
            "[[ 10 * 2 ** attempt ]]",
            "{{ 10 * 2 ** attempt }}",
            [10, 20, 40, 80, 160, 320],
        ),
    ],
    ids=["default - exponential backoff", "linear", "slow exponential backoff"],
)
async def test_actions_backoff(  # noqa: PLR0913
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    backoff: str | None,
    backoff_fixed: str | None,
    delays: list[float],
) -> None:
    """Test action service backoff parameter."""
    calls = await async_setup(hass)
    await hass.services.async_call(
        DOMAIN,
        ACTIONS_SERVICE,
        {
            CONF_SEQUENCE: BASIC_SEQUENCE_DATA,
            **({ATTR_BACKOFF: backoff} if backoff else {}),
        },
        blocking=True,
    )
    calls.pop()
    for i, delay in enumerate(delays):
        await async_next_seconds(hass, freezer, delay - 1)
        assert len(calls) == i
        await async_next_seconds(hass, freezer, 1)
        assert len(calls) == i + 1
    await async_shutdown(hass, freezer)
    if backoff:
        assert (
            f"[Failed]: attempt 7/7: {DOMAIN}.{TEST_SERVICE}()"
            f'[backoff="{backoff_fixed}"]'
        ) in caplog.text


@pytest.mark.parametrize(
    ("backoff", "error"),
    [
        ("[[ 1 ]]", None),
        ("[[ 'A' ]]", "expected float"),
        ([1], "value should be a string"),
    ],
    ids=["valid", "non number", "non string"],
)
async def test_backoff_rendered_value(
    hass: HomeAssistant,
    backoff: str,
    error: str | None,
) -> None:
    """Test backoff rendered value validation."""
    await async_setup(hass, raises=False)
    async_call = hass.services.async_call(
        DOMAIN,
        ACTION_SERVICE,
        {
            CONF_ACTION: f"{DOMAIN}.{TEST_SERVICE}",
            ATTR_BACKOFF: backoff,
        },
        blocking=True,
    )
    if not error:
        await async_call
    else:
        with pytest.raises(vol.MultipleInvalid) as exception:
            await async_call
        assert exception.value.msg == error


async def test_actions_propagating_successful_validation(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test action service with successful validation."""
    calls = await async_setup(hass, raises=False)
    await hass.services.async_call(
        DOMAIN,
        ACTIONS_SERVICE,
        {
            CONF_SEQUENCE: BASIC_SEQUENCE_DATA,
            ATTR_VALIDATION: "[[ True ]]",
        },
        blocking=True,
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 0


@pytest.mark.parametrize(
    "retry_ids",
    [["a", "b"], [None, None], ["a", None]],
    ids=["different", "disabled", "set & disabled"],
)
async def test_actions_propagating_retry_id(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, retry_ids: list[str | None]
) -> None:
    """Test action service propagating correctly the retry ID."""
    calls = await async_setup(hass)
    for i in range(2):
        await hass.services.async_call(
            DOMAIN,
            ACTIONS_SERVICE,
            {CONF_SEQUENCE: BASIC_SEQUENCE_DATA, ATTR_RETRY_ID: retry_ids[i]},
            blocking=True,
        )
    await async_shutdown(hass, freezer)
    assert len(calls) == 14  # = 7 + 7


async def test_actions_multi_calls_single_retry_id(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test retry_id with multiple service calls."""
    calls = await async_setup(hass)
    await hass.services.async_call(
        DOMAIN,
        ACTIONS_SERVICE,
        {
            ATTR_RETRY_ID: "id",
            CONF_SEQUENCE: [
                {**(BASIC_SEQUENCE_DATA[0])},
                {
                    CONF_REPEAT: {
                        CONF_COUNT: 1,
                        CONF_SEQUENCE: BASIC_SEQUENCE_DATA,
                    },
                },
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
                },
                {
                    CONF_CHOOSE: [],
                    CONF_DEFAULT: BASIC_SEQUENCE_DATA,
                },
                {
                    CONF_IF: [
                        {
                            CONF_CONDITION: "template",
                            CONF_VALUE_TEMPLATE: "{{ True }}",
                        }
                    ],
                    CONF_THEN: BASIC_SEQUENCE_DATA,
                },
                {
                    CONF_IF: [
                        {
                            CONF_CONDITION: "template",
                            CONF_VALUE_TEMPLATE: "{{ False }}",
                        }
                    ],
                    CONF_THEN: [],
                    CONF_ELSE: BASIC_SEQUENCE_DATA,
                },
                {CONF_PARALLEL: BASIC_SEQUENCE_DATA},
                {
                    CONF_PARALLEL: {CONF_SEQUENCE: BASIC_SEQUENCE_DATA},
                },
                {CONF_SEQUENCE: [{CONF_SEQUENCE: BASIC_SEQUENCE_DATA}]},
                {CONF_CONDITION: "template", CONF_VALUE_TEMPLATE: "{{True}}"},
                {**(BASIC_SEQUENCE_DATA[0])},
            ],
        },
        blocking=True,
    )
    await async_shutdown(hass, freezer)
    assert len(calls) == 70  # = 10 * 7


async def test_actions_inner_service_validation(
    hass: HomeAssistant,
) -> None:
    """Test 'actions' validate 'action' parameters."""
    await async_setup(hass)
    with pytest.raises(ServiceNotFound):
        await hass.services.async_call(
            DOMAIN,
            ACTIONS_SERVICE,
            {CONF_SEQUENCE: [{CONF_ACTION: f"{DOMAIN}.invalid"}]},
            blocking=True,
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
                        CONF_ACTION: f"{DOMAIN}.{ACTIONS_SERVICE}",
                        CONF_SERVICE_DATA: {CONF_SEQUENCE: BASIC_SEQUENCE_DATA},
                    }
                ]
            },
            blocking=True,
        )


async def test_call_in_actions(
    hass: HomeAssistant,
) -> None:
    """Test retry.action inside retry.actions."""
    await async_setup(hass)
    with pytest.raises(IntegrationError):
        await hass.services.async_call(
            DOMAIN,
            ACTIONS_SERVICE,
            {CONF_SEQUENCE: [{CONF_ACTION: f"{DOMAIN}.{ACTION_SERVICE}"}]},
            blocking=True,
        )


async def test_event_context(
    hass: HomeAssistant,
    hass_admin_user: MockUser,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the context of the events which are generated."""
    listener = Mock()
    hass.bus.async_listen(EVENT_CALL_SERVICE, listener)

    await async_setup(hass)
    context = Context(hass_admin_user.id)
    await hass.services.async_call(
        DOMAIN,
        ACTIONS_SERVICE,
        {
            CONF_SEQUENCE: BASIC_SEQUENCE_DATA,
            ATTR_RETRIES: 1,
            ATTR_ON_ERROR: [{CONF_ACTION: f"{DOMAIN}.{TEST_ON_ERROR_SERVICE}"}],
        },
        blocking=True,
        context=context,
    )
    await hass.async_block_till_done()
    await async_shutdown(hass, freezer)

    calls = [call_args.args[0] for call_args in listener.call_args_list]
    assert len(calls) == 4
    for call in calls:
        assert call.context == context
        assert call.data["domain"] == DOMAIN
    assert {call.data["service"] for call in calls} == {
        ACTIONS_SERVICE,
        ACTION_SERVICE,
        TEST_SERVICE,
        TEST_ON_ERROR_SERVICE,
    }


@pytest.mark.parametrize(
    ("backoff", "valid"),
    [
        ("[[ attempt ]]", True),
        ("{{ attempt }}", False),
        ("{% raw %}{{ attempt }}{% endraw %}", True),
    ],
    ids=["special syntax", "regular syntax - invalid", "regular syntax - wrapped"],
)
async def test_script_run_templates(
    hass: HomeAssistant,
    backoff: str,
    valid: bool,  # noqa: FBT001
) -> None:
    """Test template parameters when calling via script."""
    await async_setup(hass, raises=False)
    async_call = script.Script(
        hass,
        cv.SCRIPT_SCHEMA(
            [
                {
                    CONF_ACTION: f"{DOMAIN}.{ACTION_SERVICE}",
                    CONF_SERVICE_DATA: {
                        CONF_ACTION: f"{DOMAIN}.{TEST_SERVICE}",
                        ATTR_BACKOFF: backoff,
                    },
                }
            ]
        ),
        ACTION_SERVICE,
        DOMAIN,
    ).async_run()
    if valid:
        await async_call
    else:
        with pytest.raises(vol.MultipleInvalid) as exception:
            await async_call
        assert exception.value.msg == "length of value must be at least 1"
    await hass.async_block_till_done()
