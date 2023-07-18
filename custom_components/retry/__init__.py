"""Retry integration."""
from __future__ import annotations

import asyncio
import datetime
import logging
import voluptuous as vol
from homeassistant.components.hassio.const import ATTR_DATA
from homeassistant.components.group import DOMAIN as GROUP_DOMAIN
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_SERVICE,
    CONF_CHOOSE,
    CONF_DEFAULT,
    CONF_ELSE,
    CONF_PARALLEL,
    CONF_REPEAT,
    CONF_SEQUENCE,
    CONF_TARGET,
    CONF_THEN,
    ENTITY_MATCH_ALL,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    InvalidEntityFormatError,
    InvalidStateError,
    ServiceNotFound,
)
from homeassistant.helpers import config_validation as cv, event, script, template
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.service import async_extract_referenced_entity_ids
from homeassistant.helpers.typing import ConfigType
import homeassistant.util.dt as dt_util

from .const import (
    ACTIONS_SERVICE,
    ATTR_EXPECTED_STATE,
    ATTR_INDIVIDUALLY,
    ATTR_RETRIES,
    CALL_SERVICE,
    DOMAIN,
    LOGGER,
)

EXPONENTIAL_BACKOFF_BASE = 2
GRACE_PERIOD_FOR_STATE_UPDATE = 0.2

SERVICE_SCHEMA_BASE_FIELDS = {
    vol.Required(ATTR_RETRIES, default=7): cv.positive_int,
    vol.Optional(ATTR_EXPECTED_STATE): cv.string,
}
CALL_SERVICE_SCHEMA = vol.Schema(
    {
        **SERVICE_SCHEMA_BASE_FIELDS,
        vol.Required(ATTR_SERVICE): cv.string,
        vol.Required(ATTR_INDIVIDUALLY, default=True): cv.boolean,
    },
    extra=vol.ALLOW_EXTRA,
)

ACTIONS_SERVICE_SCHEMA = vol.Schema(
    {
        **SERVICE_SCHEMA_BASE_FIELDS,
        vol.Required(CONF_SEQUENCE): cv.SCRIPT_SCHEMA,
    },
    extra=vol.ALLOW_EXTRA,
)


def _get_entity(hass: HomeAssistant, entity_id: str) -> Entity | None:
    """Get entity object."""
    entity_domain = entity_id.split(".")[0]
    entity_comp = hass.data.get(DATA_INSTANCES, {}).get(entity_domain)
    return entity_comp.get_entity(entity_id) if entity_comp else None


class RetryParams:
    """Parse and compute input parameters."""

    def __init__(self, hass: HomeAssistant, service_call: ServiceCall) -> None:
        """Initialize the object."""
        self._hass = hass
        self.service_call = service_call
        self.retry_data = self._retry_service_data()
        self.inner_data = self._inner_service_data()
        self.service_entities = self._service_entity_ids()

    def _retry_service_data(self) -> dict[str, any]:
        """Compose retry parameters."""
        data = {}
        retry_service = template.Template(
            self.service_call.data[ATTR_SERVICE], self._hass
        ).async_render(parse_result=False)
        domain, service = retry_service.lower().split(".")
        if not self._hass.services.has_service(domain, service):
            raise ServiceNotFound(domain, service)
        data[ATTR_DOMAIN] = domain
        data[ATTR_SERVICE] = service
        data[ATTR_RETRIES] = self.service_call.data[ATTR_RETRIES]
        expected_state = self.service_call.data.get(ATTR_EXPECTED_STATE)
        if expected_state:
            data[ATTR_EXPECTED_STATE] = template.Template(
                expected_state, self._hass
            ).async_render(parse_result=False)
        data[ATTR_INDIVIDUALLY] = self.service_call.data[ATTR_INDIVIDUALLY]
        return data

    def _inner_service_data(self) -> dict[str, any]:
        """Compose inner service parameters."""
        data = {
            key: value
            for key, value in self.service_call.data.items()
            if key
            not in [ATTR_SERVICE, ATTR_RETRIES, ATTR_EXPECTED_STATE, ATTR_INDIVIDUALLY]
        }
        if schema := self._hass.services.async_services()[self.retry_data[ATTR_DOMAIN]][
            self.retry_data[ATTR_SERVICE]
        ].schema:
            schema(data)
        if data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL or (
            CONF_TARGET in data
            and data[CONF_TARGET].get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL
        ):
            raise InvalidEntityFormatError(
                f'"{ATTR_ENTITY_ID}={ENTITY_MATCH_ALL}" is not supported'
            )
        return data

    def _expand_group(self, entity_id: str) -> list[str]:
        """Return group memeber ids (when a group)."""
        entity_ids = []
        entity_obj = _get_entity(self._hass, entity_id)
        if (
            entity_obj is not None
            and entity_obj.platform is not None
            and entity_obj.platform.platform_name == GROUP_DOMAIN
        ):
            for member_id in entity_obj.extra_state_attributes.get(ATTR_ENTITY_ID, []):
                entity_ids.extend(self._expand_group(member_id))
        else:
            entity_ids.append(entity_id)
        return entity_ids

    def _service_entity_ids(self) -> list[str]:
        """Get entity ids for a service call."""
        entity_ids = []
        service_entities = async_extract_referenced_entity_ids(
            self._hass, self.service_call
        )
        for entity_id in (
            service_entities.referenced | service_entities.indirectly_referenced
        ):
            entity_ids.extend(self._expand_group(entity_id))
        return entity_ids


class RetryCall:
    """Handle a single service call with retries."""

    def __init__(
        self, hass: HomeAssistant, params: RetryParams, entity: str | None = None
    ) -> None:
        """Initialize the object."""
        self._hass = hass
        self._params = params
        self._inner_data = params.inner_data.copy()
        if entity:
            self._service_entities = [entity]
            self._set_inner_data_entities()
        else:
            self._service_entities = params.service_entities
        self._attempt = 1
        self._delay = 1

    def _set_inner_data_entities(self) -> None:
        for key in cv.ENTITY_SERVICE_FIELDS:
            if key in self._inner_data:
                del self._inner_data[key]
        self._inner_data[ATTR_ENTITY_ID] = self._service_entities

    async def _async_check_entities(self) -> None:
        """Verify that all entities are available and in the expected state."""
        invalid_entities = {}
        grace_period_for_state_update = False
        for entity_id in self._service_entities:
            if (
                ent_obj := _get_entity(self._hass, entity_id)
            ) is None or not ent_obj.available:
                invalid_entities[entity_id] = f"{entity_id} is not available"
            elif ATTR_EXPECTED_STATE in self._params.retry_data:
                if (state := ent_obj.state) != self._params.retry_data[
                    ATTR_EXPECTED_STATE
                ]:
                    if not grace_period_for_state_update:
                        await asyncio.sleep(GRACE_PERIOD_FOR_STATE_UPDATE)
                        state = ent_obj.state
                        grace_period_for_state_update = True
                    if state != self._params.retry_data[ATTR_EXPECTED_STATE]:
                        invalid_entities[entity_id] = (
                            f'{entity_id} state is "{state}" but '
                            f'expecting "{self._params.retry_data[ATTR_EXPECTED_STATE]}"'
                        )
        if invalid_entities:
            self._service_entities = list(invalid_entities.keys())
            self._set_inner_data_entities()
            raise InvalidStateError("; ".join(invalid_entities.values()))

    def _log(self, level: int, prefix: str, stack_info: bool = False) -> None:
        """Log entry."""
        LOGGER.log(
            level,
            "[%s]: attempt #%d, retry_data=%s, inner_data=%s, entities=%s",
            prefix,
            self._attempt,
            self._params.retry_data,
            self._inner_data,
            self._service_entities,
            exc_info=stack_info,
        )

    @callback
    async def async_retry(self, *_) -> bool:
        """One service call attempt."""
        try:
            await self._hass.services.async_call(
                self._params.retry_data[ATTR_DOMAIN],
                self._params.retry_data[ATTR_SERVICE],
                self._inner_data.copy(),
                True,
                self._params.service_call.context,
            )
            await self._async_check_entities()
            self._log(
                logging.DEBUG if self._attempt == 1 else logging.INFO, "Succeeded"
            )
            return
        except Exception:  # pylint: disable=broad-except
            self._log(
                logging.WARNING
                if self._attempt < self._params.retry_data[ATTR_RETRIES]
                else logging.ERROR,
                "Failed",
                True,
            )
        if self._attempt == self._params.retry_data[ATTR_RETRIES]:
            return
        next_retry = dt_util.now() + datetime.timedelta(seconds=self._delay)
        self._delay *= EXPONENTIAL_BACKOFF_BASE
        self._attempt += 1
        event.async_track_point_in_time(self._hass, self.async_retry, next_retry)


def _wrap_service_calls(sequence: list[dict], retry_params: dict[str, any]) -> None:
    """Warp any service call with retry."""
    for action in sequence:
        match cv.determine_script_action(action):
            case cv.SCRIPT_ACTION_CALL_SERVICE:
                action[ATTR_DATA] = action.get(ATTR_DATA, {})
                action[ATTR_DATA][ATTR_SERVICE] = action[ATTR_SERVICE]
                action[ATTR_DATA].update(retry_params)
                action[ATTR_SERVICE] = f"{DOMAIN}.{CALL_SERVICE}"
            case cv.SCRIPT_ACTION_REPEAT:
                _wrap_service_calls(action[CONF_REPEAT][CONF_SEQUENCE], retry_params)
            case cv.SCRIPT_ACTION_CHOOSE:
                for choose in action[CONF_CHOOSE]:
                    _wrap_service_calls(choose[CONF_SEQUENCE], retry_params)
                if CONF_DEFAULT in action:
                    _wrap_service_calls(action[CONF_DEFAULT], retry_params)
            case cv.SCRIPT_ACTION_IF:
                _wrap_service_calls(action[CONF_THEN], retry_params)
                if CONF_ELSE in action:
                    _wrap_service_calls(action[CONF_ELSE], retry_params)
            case cv.SCRIPT_ACTION_PARALLEL:
                for parallel in action[CONF_PARALLEL]:
                    _wrap_service_calls(parallel[CONF_SEQUENCE], retry_params)


async def async_setup_entry(hass: HomeAssistant, _: ConfigEntry) -> bool:
    """Set up domain."""

    async def async_call(service_call: ServiceCall) -> None:
        """Call service with background retries."""
        params = RetryParams(hass, service_call)
        entities = params.service_entities
        if (
            not params.retry_data[ATTR_INDIVIDUALLY]
            or len(params.service_entities) <= 1
        ):
            entities = [None]
        for entity in entities:
            hass.async_create_task(RetryCall(hass, params, entity).async_retry())

    hass.services.async_register(DOMAIN, CALL_SERVICE, async_call, CALL_SERVICE_SCHEMA)

    async def async_actions(service_call: ServiceCall) -> None:
        """Execute actions and retry failed service calls."""
        sequence = service_call.data[CONF_SEQUENCE].copy()
        retry_params = {
            key: value
            for key, value in service_call.data.items()
            if key in (ATTR_RETRIES, ATTR_EXPECTED_STATE, ATTR_INDIVIDUALLY)
        }
        _wrap_service_calls(sequence, retry_params)
        await script.Script(hass, sequence, ACTIONS_SERVICE, DOMAIN).async_run(
            context=service_call.context
        )

    hass.services.async_register(
        DOMAIN, ACTIONS_SERVICE, async_actions, ACTIONS_SERVICE_SCHEMA
    )

    return True


async def async_setup(hass: HomeAssistant, _: ConfigType) -> bool:
    """Create config entry from configuration.yaml."""
    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}
            )
        )
    return True


async def async_unload_entry(hass: HomeAssistant, _: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, CALL_SERVICE)
    hass.services.async_remove(DOMAIN, ACTIONS_SERVICE)
    return True
