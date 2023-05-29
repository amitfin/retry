"""Retry integration."""
from __future__ import annotations

import asyncio
import datetime
import logging
import voluptuous as vol
from homeassistant.components.group import DOMAIN as GROUP_DOMAIN
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_SERVICE,
    CONF_TARGET,
    ENTITY_MATCH_ALL,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    HomeAssistantError,
    InvalidEntityFormatError,
    InvalidStateError,
    ServiceNotFound,
)
from homeassistant.helpers import config_validation as cv, event, template
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.service import async_extract_referenced_entity_ids
from homeassistant.helpers.typing import ConfigType
import homeassistant.util.dt as dt_util

from .const import ATTR_EXPECTED_STATE, ATTR_RETRIES, DOMAIN, LOGGER, SERVICE

EXPONENTIAL_BACKOFF_BASE = 2
GRACE_PERIOD_FOR_STATE_UPDATE = 0.2

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SERVICE): cv.string,
        vol.Required(ATTR_RETRIES, default=7): cv.positive_int,
        vol.Optional(ATTR_EXPECTED_STATE): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)

class RetryCall:
    """Handle a single service call with retries."""

    def __init__(self, hass: HomeAssistant, service_call: ServiceCall) -> None:
        """Initialize the object."""
        self._hass = hass
        self._service_call = service_call
        self._retry_data = self._retry_service_data()
        self._inner_data = self._inner_service_data()
        self._service_entities = self._service_entity_ids()
        self._attempt = 1
        self._delay = 1

    def _retry_service_data(self) -> dict[str, any]:
        """Compose retry parameters."""
        data = {}
        retry_service = template.Template(
            self._service_call.data[ATTR_SERVICE], self._hass
        ).async_render(parse_result=False)
        domain, service = retry_service.lower().split(".")
        if not self._hass.services.has_service(domain, service):
            raise ServiceNotFound(domain, service)
        data[ATTR_DOMAIN] = domain
        data[ATTR_SERVICE] = service
        data[ATTR_RETRIES] = self._service_call.data[ATTR_RETRIES]
        expected_state = self._service_call.data.get(ATTR_EXPECTED_STATE)
        if expected_state:
            data[ATTR_EXPECTED_STATE] = template.Template(
                expected_state, self._hass
            ).async_render(parse_result=False)
        return data

    def _inner_service_data(self) -> dict[str, any]:
        """Compose inner service parameters."""
        data = {
            key: value
            for key, value in self._service_call.data.items()
            if key not in [ATTR_SERVICE, ATTR_RETRIES, ATTR_EXPECTED_STATE]
        }
        if schema := self._hass.services.async_services()[self._retry_data[ATTR_DOMAIN]][
            self._retry_data[ATTR_SERVICE]].schema:
            schema(data)
        if data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL or (
            CONF_TARGET in data
            and data[CONF_TARGET].get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL
        ):
            raise InvalidEntityFormatError(
                f'"{ATTR_ENTITY_ID}={ENTITY_MATCH_ALL}" is not supported'
            )
        return data

    def _get_entity(self, entity_id: str) -> Entity | None:
        """Get entity object."""
        entity_domain = entity_id.split(".")[0]
        entity_comp = self._hass.data.get(DATA_INSTANCES, {}).get(entity_domain)
        return entity_comp.get_entity(entity_id) if entity_comp else None

    def _expand_group(self, entity_id: str) -> list[str]:
        """Return group memeber ids (when a group)."""
        entity_ids = []
        entity_obj = self._get_entity(entity_id)
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
        service_entities = async_extract_referenced_entity_ids(self._hass, self._service_call)
        for entity_id in (
            service_entities.referenced | service_entities.indirectly_referenced
        ):
            entity_ids.extend(self._expand_group(entity_id))
        return entity_ids

    async def _async_check_entities(self) -> None:
        """Verify that all entities are available and in the expected state."""
        invalid_entities = {}
        grace_period_for_state_update = False
        for entity_id in self._service_entities:
            if (ent_obj := self._get_entity(entity_id)) is None or not ent_obj.available:
                invalid_entities[entity_id] = f"{entity_id} is not available"
            elif ATTR_EXPECTED_STATE in self._retry_data:
                if (state := ent_obj.state) != self._retry_data[ATTR_EXPECTED_STATE]:
                    if not grace_period_for_state_update:
                        await asyncio.sleep(GRACE_PERIOD_FOR_STATE_UPDATE)
                        state = ent_obj.state
                        grace_period_for_state_update = True
                    if state != self._retry_data[ATTR_EXPECTED_STATE]:
                        invalid_entities[entity_id] = (
                            f'{entity_id} state is "{state}" but '
                            f'expecting "{self._retry_data[ATTR_EXPECTED_STATE]}"'
                        )
        if invalid_entities:
            for key in cv.ENTITY_SERVICE_FIELDS:
                if key in self._inner_data:
                    del self._inner_data[key]
            self._inner_data[ATTR_ENTITY_ID] = self._service_entities = list(
                invalid_entities.keys()
            )
            raise InvalidStateError("; ".join(invalid_entities.values()))

    def _log(self, level: int, prefix: str, stack_info: bool = False) -> None:
        """Log entry."""
        LOGGER.log(
            level,
            "[%s]: attempt #%d, retry_data=%s, inner_data=%s, entities=%s",
            prefix,
            self._attempt,
            self._retry_data,
            self._inner_data,
            self._service_entities,
            exc_info=stack_info,
        )

    @callback
    async def async_retry(self, *_) -> bool:
        """One service call attempt."""
        try:
            if (
                await self._hass.services.async_call(
                    self._retry_data[ATTR_DOMAIN],
                    self._retry_data[ATTR_SERVICE],
                    self._inner_data.copy(),
                    True,
                    self._service_call.context,
                )
                is False
            ):
                raise HomeAssistantError("ServiceRegistry.async_call failed")
            await self._async_check_entities()
            self._log(logging.DEBUG if self._attempt == 1 else logging.INFO, "Succeeded")
            return
        except Exception:  # pylint: disable=broad-except
            self._log(
                logging.WARNING
                if self._attempt < self._retry_data[ATTR_RETRIES]
                else logging.ERROR,
                "Failed",
                True,
            )
        if self._attempt == self._retry_data[ATTR_RETRIES]:
            return
        next_retry = dt_util.now() + datetime.timedelta(seconds=self._delay)
        self._delay *= EXPONENTIAL_BACKOFF_BASE
        self._attempt += 1
        event.async_track_point_in_time(self._hass, self.async_retry, next_retry)


async def async_setup_entry(hass: HomeAssistant, _: ConfigEntry) -> bool:
    """Set up domain."""

    async def async_call(service_call: ServiceCall) -> None:
        """Call service with background retries."""
        await RetryCall(hass, service_call).async_retry()

    hass.services.async_register(DOMAIN, SERVICE, async_call, SERVICE_SCHEMA)
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
    hass.services.async_remove(DOMAIN, SERVICE)
    return True
