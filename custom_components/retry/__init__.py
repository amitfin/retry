"""Retry integration."""
from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
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
from homeassistant.core import Context, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    IntegrationError,
    InvalidStateError,
    ServiceNotFound,
)
from homeassistant.helpers import (
    config_validation as cv,
    event,
    issue_registry as ir,
    script,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import DATA_INSTANCES, EntityComponent
from homeassistant.helpers.service import async_extract_referenced_entity_ids
from homeassistant.helpers.template import Template, result_as_boolean
from homeassistant.helpers.typing import ConfigType
import homeassistant.util.dt as dt_util

from .const import (
    ACTIONS_SERVICE,
    ATTR_EXPECTED_STATE,
    ATTR_RETRIES,
    ATTR_STATE_GRACE,
    ATTR_VALIDATION,
    CALL_SERVICE,
    CONF_DISABLE_REPAIR,
    DOMAIN,
    LOGGER,
)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

EXPONENTIAL_BACKOFF_BASE = 2
DEFAULT_RETRIES = 7
DEFAULT_STATE_GRACE = 0.2


def _template_parameter(value: any | None) -> str:
    """Render template parameter."""
    output = cv.template(value).async_render(parse_result=False)
    if not isinstance(output, str):
        raise vol.Invalid("template rendered value should be a string")
    return output


def _fix_template_tokens(value: str) -> str:
    """Replace template's artificial tokens brackets with Jinja's valid tokens."""
    for artificial, valid in {
        "[[": "{{",
        "]]": "}}",
        "[%": "{%",
        "%]": "%}",
        "[#": "{#",
        "#]": "#}",
    }.items():
        value = value.replace(artificial, valid)
    return value


def _validation_parameter(value: any | None) -> Template:
    """Convert validation parameter to template."""
    return cv.dynamic_template(_fix_template_tokens(cv.string(value)))


SERVICE_SCHEMA_BASE_FIELDS = {
    vol.Required(ATTR_RETRIES, default=DEFAULT_RETRIES): cv.positive_int,
    vol.Optional(ATTR_EXPECTED_STATE): vol.All(cv.ensure_list, [_template_parameter]),
    vol.Optional(ATTR_VALIDATION): _validation_parameter,
    vol.Required(ATTR_STATE_GRACE, default=DEFAULT_STATE_GRACE): cv.positive_float,
}
CALL_SERVICE_SCHEMA = vol.Schema(
    {
        **SERVICE_SCHEMA_BASE_FIELDS,
        vol.Required(ATTR_SERVICE): _template_parameter,
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


def _get_entity_component(hass: HomeAssistant, domain: str) -> EntityComponent | None:
    """Get entity component object."""
    return hass.data.get(DATA_INSTANCES, {}).get(domain)


def _get_entity(hass: HomeAssistant, entity_id: str) -> Entity | None:
    """Get entity object."""
    entity_comp = _get_entity_component(hass, entity_id.split(".")[0])
    return entity_comp.get_entity(entity_id) if entity_comp else None


class RetryParams:
    """Parse and compute input parameters."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry | None,
        data: dict[str, any],
    ) -> None:
        """Initialize the object."""
        self.config_entry = config_entry
        self.retry_data = self._retry_service_data(hass, data)
        self.inner_data = self._inner_service_data(hass, data)
        self.service_entities = self._service_entity_ids(hass)

    @staticmethod
    def _retry_service_data(
        hass: HomeAssistant, data: dict[str, any]
    ) -> dict[str, any]:
        """Compose retry parameters."""
        retry_data = {
            key: data[key] for key in data if key in SERVICE_SCHEMA_BASE_FIELDS
        }
        retry_service = data[ATTR_SERVICE]
        domain, service = retry_service.lower().split(".")
        if not hass.services.has_service(domain, service):
            raise ServiceNotFound(domain, service)
        retry_data[ATTR_DOMAIN] = domain
        retry_data[ATTR_SERVICE] = service
        return retry_data

    def _inner_service_data(
        self, hass: HomeAssistant, data: dict[str, any]
    ) -> dict[str, any]:
        """Compose inner service parameters."""
        inner_data = {
            key: value
            for key, value in data.items()
            if key not in CALL_SERVICE_SCHEMA.schema
        }
        if hasattr(hass.services, "async_services_for_domain"):
            domain_services = hass.services.async_services_for_domain(
                self.retry_data[ATTR_DOMAIN]
            )
        else:
            domain_services = hass.services.async_services()[
                self.retry_data[ATTR_DOMAIN]
            ]
        if schema := domain_services[self.retry_data[ATTR_SERVICE]].schema:
            schema(inner_data)
        return inner_data

    def _expand_group(self, hass: HomeAssistant, entity_id: str) -> list[str]:
        """Return group member ids (when a group)."""
        entity_ids = []
        entity_obj = _get_entity(hass, entity_id)
        if (
            entity_obj is not None
            and entity_obj.platform is not None
            and entity_obj.platform.platform_name == GROUP_DOMAIN
        ):
            for member_id in entity_obj.extra_state_attributes.get(ATTR_ENTITY_ID, []):
                entity_ids.extend(self._expand_group(hass, member_id))
        else:
            entity_ids.append(entity_id)
        return entity_ids

    def _service_entity_ids(self, hass: HomeAssistant) -> list[str]:
        """Get entity ids for a service call."""
        if self.inner_data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL:
            # Assuming it's a component (domain) service and not platform specific.
            # AFAIK, it's not possible to get the platform by the service name.
            entity_comp = _get_entity_component(hass, self.retry_data[ATTR_DOMAIN])
            return [
                entity.entity_id
                for entity in (entity_comp.entities if entity_comp else [])
            ]
        entity_ids = []
        service_entities = async_extract_referenced_entity_ids(
            hass,
            ServiceCall(
                self.retry_data[ATTR_DOMAIN],
                self.retry_data[ATTR_SERVICE],
                self.inner_data,
            ),
        )
        for entity_id in (
            service_entities.referenced | service_entities.indirectly_referenced
        ):
            entity_ids.extend(self._expand_group(hass, entity_id))
        return entity_ids


class RetryCall:
    """Handle a single service call with retries."""

    def __init__(
        self,
        hass: HomeAssistant,
        params: RetryParams,
        context: Context,
        entity_id: str | None = None,
    ) -> None:
        """Initialize the object."""
        self._hass = hass
        self._params = params
        self._inner_data = params.inner_data.copy()
        if entity_id:
            for key in cv.ENTITY_SERVICE_FIELDS:
                if key in self._inner_data:
                    del self._inner_data[key]
            self._inner_data = {
                **{ATTR_ENTITY_ID: entity_id},
                **self._inner_data,
            }
        self._entity_id = entity_id
        self._context = context
        self._attempt = 1
        self._delay = 1

    async def _async_validate(self) -> None:
        """Verify that the entity is available, in the expected state, and pass the validation."""
        if self._entity_id:
            if (
                ent_obj := _get_entity(self._hass, self._entity_id)
            ) is None or not ent_obj.available:
                raise InvalidStateError(f"{self._entity_id} is not available")
        else:
            ent_obj = None
        if not self._check_state(ent_obj) or not self._check_validation():
            await asyncio.sleep(self._params.retry_data[ATTR_STATE_GRACE])
            if not self._check_state(ent_obj):
                raise InvalidStateError(
                    f'{self._entity_id} state is "{ent_obj.state}" but '
                    f'expecting one of "{self._params.retry_data[ATTR_EXPECTED_STATE]}"'
                )
            if not self._check_validation():
                raise InvalidStateError(
                    f'"{self._params.retry_data[ATTR_VALIDATION].template}" is False'
                )

    def _check_state(self, entity: Entity | None) -> bool:
        """Check if the entity's state is expected."""
        if not entity or ATTR_EXPECTED_STATE not in self._params.retry_data:
            return True
        for expected in self._params.retry_data[ATTR_EXPECTED_STATE]:
            if entity.state == expected:
                return True
            try:
                if float(entity.state) == float(expected):
                    return True
            except ValueError:
                pass
        return False

    def _check_validation(self) -> bool:
        """Check if the validation statement is true."""
        if ATTR_VALIDATION not in self._params.retry_data:
            return True
        return result_as_boolean(
            self._params.retry_data[ATTR_VALIDATION].async_render(
                variables={"entity_id": self._entity_id} if self._entity_id else None
            )
        )

    def _service_call_str(self) -> str:
        """Return a string with the service call parameters."""
        service_call = (
            f"{self._params.retry_data[ATTR_DOMAIN]}.{self._params.retry_data[ATTR_SERVICE]}"
            f"({', '.join([f'{key}={value}' for key, value in self._inner_data.items()])})"
        )
        retry_params = []
        if (
            expected_state := self._params.retry_data.get(ATTR_EXPECTED_STATE)
        ) is not None:
            if len(expected_state) == 1:
                retry_params.append(f"expected_state={expected_state[0]}")
            else:
                retry_params.append(
                    f"expected_state in ({', '.join(state for state in expected_state)})"
                )
        if (validation := self._params.retry_data.get(ATTR_VALIDATION)) is not None:
            retry_params.append(f'validation="{validation.template}"')
        if self._params.retry_data[ATTR_STATE_GRACE] != DEFAULT_STATE_GRACE:
            retry_params.append(
                f"state_grace={self._params.retry_data[ATTR_STATE_GRACE]}"
            )
        if len(retry_params) > 0:
            service_call += f"[{', '.join(retry_params)}]"
        return service_call

    def _log(self, level: int, prefix: str, stack_info: bool = False) -> None:
        """Log entry."""
        LOGGER.log(
            level,
            "[%s]: attempt %d/%d: %s",
            prefix,
            self._attempt,
            self._params.retry_data[ATTR_RETRIES],
            self._service_call_str(),
            exc_info=stack_info,
        )

    def _repair(self) -> None:
        """Create a repair ticket."""
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            f"retry_{uuid.uuid4()}",
            is_fixable=False,
            learn_more_url="https://github.com/amitfin/retry#retrycall",
            severity=ir.IssueSeverity.ERROR,
            translation_key="failure",
            translation_placeholders={
                "service": self._service_call_str(),
                "retries": self._params.retry_data[ATTR_RETRIES],
            },
        )

    @callback
    async def async_retry(self, *_) -> None:
        """One service call attempt."""
        try:
            await self._hass.services.async_call(
                self._params.retry_data[ATTR_DOMAIN],
                self._params.retry_data[ATTR_SERVICE],
                self._inner_data.copy(),
                True,
                Context(self._context.user_id, self._context.id),
            )
            await self._async_validate()
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
            if not self._params.config_entry.options.get(CONF_DISABLE_REPAIR):
                self._repair()
            return
        next_retry = dt_util.now() + datetime.timedelta(seconds=self._delay)
        self._delay *= EXPONENTIAL_BACKOFF_BASE
        self._attempt += 1
        event.async_track_point_in_time(self._hass, self.async_retry, next_retry)


def _wrap_service_calls(
    hass: HomeAssistant, sequence: list[dict], retry_params: dict[str, any]
) -> None:
    """Warp any service call with retry."""
    for action in sequence:
        match cv.determine_script_action(action):
            case cv.SCRIPT_ACTION_CALL_SERVICE:
                if action[ATTR_SERVICE] == f"{DOMAIN}.{ACTIONS_SERVICE}":
                    raise IntegrationError("Nested retry.actions are disallowed")
                if action[ATTR_SERVICE] == f"{DOMAIN}.{CALL_SERVICE}":
                    raise IntegrationError(
                        "retry.call inside retry.actions is disallowed"
                    )
                action[ATTR_DATA] = action.get(ATTR_DATA, {})
                action[ATTR_DATA][ATTR_SERVICE] = action[ATTR_SERVICE]
                action[ATTR_DATA].update(retry_params)
                action[ATTR_SERVICE] = f"{DOMAIN}.{CALL_SERVICE}"
                # Validate parameters so errors are not raised in the background.
                RetryParams(
                    hass,
                    None,
                    {**action[ATTR_DATA], **action.get(CONF_TARGET, {})},
                )
            case cv.SCRIPT_ACTION_REPEAT:
                _wrap_service_calls(
                    hass, action[CONF_REPEAT][CONF_SEQUENCE], retry_params
                )
            case cv.SCRIPT_ACTION_CHOOSE:
                for choose in action[CONF_CHOOSE]:
                    _wrap_service_calls(hass, choose[CONF_SEQUENCE], retry_params)
                if CONF_DEFAULT in action:
                    _wrap_service_calls(hass, action[CONF_DEFAULT], retry_params)
            case cv.SCRIPT_ACTION_IF:
                _wrap_service_calls(hass, action[CONF_THEN], retry_params)
                if CONF_ELSE in action:
                    _wrap_service_calls(hass, action[CONF_ELSE], retry_params)
            case cv.SCRIPT_ACTION_PARALLEL:
                for parallel in action[CONF_PARALLEL]:
                    _wrap_service_calls(hass, parallel[CONF_SEQUENCE], retry_params)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up domain."""

    async def async_call(service_call: ServiceCall) -> None:
        """Call service with background retries."""
        params = RetryParams(hass, config_entry, service_call.data)
        for entity_id in params.service_entities or [None]:
            hass.async_create_task(
                RetryCall(hass, params, service_call.context, entity_id).async_retry()
            )

    hass.services.async_register(DOMAIN, CALL_SERVICE, async_call, CALL_SERVICE_SCHEMA)

    async def async_actions(service_call: ServiceCall) -> None:
        """Execute actions and retry failed service calls."""
        sequence = service_call.data[CONF_SEQUENCE].copy()
        retry_params = {
            key: service_call.data[key]
            for key in service_call.data
            if key in SERVICE_SCHEMA_BASE_FIELDS
        }
        if ATTR_VALIDATION in retry_params:
            # Revert it back to string so it won't get rendered in advance.
            retry_params[ATTR_VALIDATION] = retry_params[ATTR_VALIDATION].template
        _wrap_service_calls(hass, sequence, retry_params)
        await script.Script(hass, sequence, ACTIONS_SERVICE, DOMAIN).async_run(
            context=Context(service_call.context.user_id, service_call.context.id)
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
