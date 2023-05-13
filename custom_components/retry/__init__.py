"""Retry integration."""
from __future__ import annotations

import datetime
import voluptuous as vol
from homeassistant.components.group import DOMAIN as GROUP_DOMAIN
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SERVICE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    HomeAssistantError,
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

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SERVICE): cv.string,
        vol.Required(ATTR_RETRIES, default=7): cv.positive_int,
        vol.Optional(ATTR_EXPECTED_STATE): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_entry(hass: HomeAssistant, _: ConfigEntry) -> bool:
    """Set up domain."""

    def get_entity(entity_id: str) -> Entity | None:
        """Get entity object."""
        entity_domain = entity_id.split(".")[0]
        entity_comp = hass.data.get(DATA_INSTANCES, {}).get(entity_domain)
        return entity_comp.get_entity(entity_id) if entity_comp else None

    def expand_group(entity_id: str) -> list[str]:
        """Return group memeber ids (when a group)."""
        entity_ids = []
        entity_obj = get_entity(entity_id)
        if (
            entity_obj is not None
            and entity_obj.platform is not None
            and entity_obj.platform.platform_name == GROUP_DOMAIN
        ):
            for member_id in entity_obj.extra_state_attributes.get(ATTR_ENTITY_ID, []):
                entity_ids.extend(expand_group(member_id))
        else:
            entity_ids.append(entity_id)
        return entity_ids

    def service_entity_ids(service_call: ServiceCall) -> list[str]:
        """Get entity ids for a service call."""
        entity_ids = []
        service_entities = async_extract_referenced_entity_ids(hass, service_call)
        for entity_id in (
            service_entities.referenced | service_entities.indirectly_referenced
        ):
            entity_ids.extend(expand_group(entity_id))
        return entity_ids

    async def async_call(service_call: ServiceCall) -> None:
        """Call service with background retries."""
        service_entities = service_entity_ids(service_call)

        service_data = service_call.data.copy()
        retry_service = template.Template(
            service_data[ATTR_SERVICE], hass
        ).async_render(parse_result=False)
        domain, service = retry_service.lower().split(".")
        del service_data[ATTR_SERVICE]
        if not hass.services.has_service(domain, service):
            raise ServiceNotFound(domain, service)
        max_retries = service_data[ATTR_RETRIES]
        del service_data[ATTR_RETRIES]
        expected_state = service_data.get(ATTR_EXPECTED_STATE)
        if expected_state:
            expected_state = template.Template(expected_state, hass).async_render(
                parse_result=False
            )
            del service_data[ATTR_EXPECTED_STATE]

        schema = hass.services.async_services()[domain][service].schema
        if schema:
            schema(service_data)

        retries = 1
        delay = 1
        call = f"{domain}.{service}(data={service_data})"
        LOGGER.debug(
            "Calling %s, entity_ids=%s, max_retries=%d, expected_state=%s",
            call,
            service_entities,
            max_retries,
            expected_state,
        )

        async def async_check_entities() -> None:
            """Verify that all entities are available and in the expected state."""
            for entity_id in service_entities:
                if (ent_obj := get_entity(entity_id)) is None or not ent_obj.available:
                    raise InvalidStateError(f"{entity_id} is not available")
                if expected_state:
                    await hass.async_block_till_done()
                    if (state := ent_obj.state) != expected_state:
                        raise InvalidStateError(
                            f'{entity_id} state is "{state}" but expecting "{expected_state}"'
                        )

        @callback
        async def async_retry(*_) -> bool:
            """One service call attempt."""
            nonlocal retries
            nonlocal delay
            try:
                if retries > 1:
                    LOGGER.info("Calling (%d/%d): %s", retries, max_retries, call)
                if (
                    await hass.services.async_call(
                        domain, service, service_data.copy(), True, service_call.context
                    )
                    is False
                ):
                    raise HomeAssistantError("ServiceRegistry.async_call failed")
                await async_check_entities()
                if retries == 1:
                    LOGGER.debug("Succeeded: %s", call)
                else:
                    LOGGER.info("Succeeded (%d/%d): %s", retries, max_retries, call)
                return
            except Exception:  # pylint: disable=broad-except
                LOGGER.warning(
                    "%s attempt #%d (of %d) failed",
                    call,
                    retries,
                    max_retries,
                    exc_info=True,
                )
            if retries == max_retries:
                LOGGER.error("Failed: %s", call)
                return
            next_retry = dt_util.now() + datetime.timedelta(seconds=delay)
            delay *= EXPONENTIAL_BACKOFF_BASE
            retries += 1
            event.async_track_point_in_time(hass, async_retry, next_retry)

        await async_retry()

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
