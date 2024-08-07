"""Retry integration."""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from typing import TYPE_CHECKING, Any

import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.components.hassio.const import ATTR_DATA
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_SERVICE,
    CONF_ACTION,
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
)
from homeassistant.helpers import (
    event,
    script,
)
from homeassistant.helpers import (
    issue_registry as ir,
)
from homeassistant.helpers.entity_component import DATA_INSTANCES, EntityComponent
from homeassistant.helpers.service import async_extract_referenced_entity_ids
from homeassistant.helpers.template import Template, result_as_boolean

if TYPE_CHECKING:
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.typing import ConfigType

from .const import (
    ACTION_SERVICE,
    ACTIONS_SERVICE,
    ATTR_BACKOFF,
    ATTR_EXPECTED_STATE,
    ATTR_ON_ERROR,
    ATTR_RETRIES,
    ATTR_RETRY_ID,
    ATTR_STATE_DELAY,
    ATTR_STATE_GRACE,
    ATTR_VALIDATION,
    CALL_SERVICE,
    CONF_DISABLE_REPAIR,
    DOMAIN,
    LOGGER,
)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

DEFAULT_BACKOFF = "[[ 2 ** attempt ]]"
DEFAULT_RETRIES = 7
DEFAULT_STATE_GRACE = 0.2
GROUP_DOMAIN = "group"

_running_retries: dict[str, tuple[str, int]] = {}
_running_retries_write_lock = threading.Lock()


def _template_parameter(value: Any) -> str:
    """Render template parameter."""
    output = cv.template(value).async_render(parse_result=False)
    if not isinstance(output, str):
        message = "template rendered value should be a string"
        raise vol.Invalid(message)
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


DEFAULT_BACKOFF_FIXED = _fix_template_tokens(DEFAULT_BACKOFF)


def _backoff_parameter(value: Any | None) -> Template:
    """Convert backoff parameter to template."""
    return cv.template(_fix_template_tokens(cv.string(value)))


def _validation_parameter(value: Any | None) -> Template:
    """Convert validation parameter to template."""
    return cv.dynamic_template(_fix_template_tokens(cv.string(value)))


def _rename_legacy_service_key(value: Any | None) -> Any:
    if not isinstance(value, dict):
        return value
    if ATTR_SERVICE in value:
        value[CONF_ACTION] = value.pop(ATTR_SERVICE)
    return value


SERVICE_SCHEMA_BASE_FIELDS = {
    vol.Required(ATTR_RETRIES, default=DEFAULT_RETRIES): cv.positive_int,  # type: ignore[reportArgumentType]
    vol.Required(ATTR_BACKOFF, default=DEFAULT_BACKOFF): _backoff_parameter,  # type: ignore[reportArgumentType]
    vol.Optional(ATTR_EXPECTED_STATE): vol.All(cv.ensure_list, [_template_parameter]),
    vol.Optional(ATTR_VALIDATION): _validation_parameter,
    vol.Required(ATTR_STATE_DELAY, default=0): cv.positive_float,  # type: ignore[reportArgumentType]
    vol.Required(ATTR_STATE_GRACE, default=DEFAULT_STATE_GRACE): cv.positive_float,  # type: ignore[reportArgumentType]
    vol.Optional(ATTR_RETRY_ID): vol.Any(cv.string, None),
    vol.Optional(ATTR_ON_ERROR): cv.SCRIPT_SCHEMA,
}
ACTION_SERVICE_PARAMS = vol.Schema(
    {
        **SERVICE_SCHEMA_BASE_FIELDS,
        vol.Optional(ATTR_SERVICE): _template_parameter,
        vol.Optional(CONF_ACTION): _template_parameter,
    },
    extra=vol.ALLOW_EXTRA,
)
ACTION_SERVICE_SCHEMA = vol.All(
    cv.has_at_least_one_key(ATTR_SERVICE, CONF_ACTION),
    cv.has_at_most_one_key(ATTR_SERVICE, CONF_ACTION),
    _rename_legacy_service_key,
    ACTION_SERVICE_PARAMS,
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
        data: dict[str, Any],
    ) -> None:
        """Initialize the object."""
        self.config_entry = config_entry
        self.retry_data = self._retry_data(hass, data)
        self.inner_data = self._inner_data(hass, data)
        self.entities = self._entity_ids(hass)

    @staticmethod
    def _retry_data(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
        """Compose retry parameters."""
        retry_data: dict[str, Any] = {
            key: data[key] for key in data if key in SERVICE_SCHEMA_BASE_FIELDS
        }
        retry_action = data[CONF_ACTION]
        domain, service = retry_action.lower().split(".")
        if not hass.services.has_service(domain, service):
            raise ServiceNotFound(domain, service)
        retry_data[ATTR_DOMAIN] = domain
        retry_data[ATTR_SERVICE] = service
        return retry_data

    def _inner_data(self, hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
        """Compose inner action parameters."""
        inner_data = {
            key: value
            for key, value in data.items()
            if key not in ACTION_SERVICE_PARAMS.schema
        }
        domain_services = hass.services.async_services_for_domain(
            self.retry_data[ATTR_DOMAIN]
        )
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
            for member_id in getattr(entity_obj, "extra_state_attributes", {}).get(
                ATTR_ENTITY_ID, []
            ):
                entity_ids.extend(self._expand_group(hass, member_id))
        else:
            entity_ids.append(entity_id)
        return entity_ids

    def _entity_ids(self, hass: HomeAssistant) -> list[str]:
        """Extract and expand entity ids."""
        if self.inner_data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL:
            # Assuming it's a component (domain) service and not platform specific.
            # AFAIK, it's not possible to get the platform by the service name.
            entity_comp = _get_entity_component(hass, self.retry_data[ATTR_DOMAIN])
            return [
                entity.entity_id
                for entity in (entity_comp.entities if entity_comp else [])
            ]
        entity_ids = []
        entities = async_extract_referenced_entity_ids(
            hass,
            ServiceCall(
                self.retry_data[ATTR_DOMAIN],
                self.retry_data[ATTR_SERVICE],
                self.inner_data,
            ),
        )
        for entity_id in entities.referenced | entities.indirectly_referenced:
            entity_ids.extend(self._expand_group(hass, entity_id))
        return entity_ids


class RetryAction:
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
                ATTR_ENTITY_ID: entity_id,
                **self._inner_data,
            }
        self._entity_id = entity_id
        self._context = context
        self._attempt = 1
        self._retry_id = params.retry_data.get(ATTR_RETRY_ID)
        if ATTR_RETRY_ID not in params.retry_data:
            if self._entity_id:
                self._retry_id = self._entity_id
            else:
                self._retry_id = (
                    f"{params.retry_data[ATTR_DOMAIN]}."
                    + params.retry_data[ATTR_SERVICE]
                )
        self._action_str_value = None
        self._start_id()

    async def _async_validate(self) -> None:
        """Check the entity is available has expected state and pass validation."""
        if self._entity_id:
            if (
                ent_obj := _get_entity(self._hass, self._entity_id)
            ) is None or not ent_obj.available:
                message = f"{self._entity_id} is not available"
                raise InvalidStateError(message)
        else:
            ent_obj = None
        if (state_delay := self._params.retry_data[ATTR_STATE_DELAY]) > 0:
            await asyncio.sleep(state_delay)
        if not self._check_state(ent_obj) or not self._check_validation():
            await asyncio.sleep(self._params.retry_data[ATTR_STATE_GRACE])
            if not self._check_state(ent_obj):
                message = (
                    f'{self._entity_id} state is "{getattr(ent_obj, "state", "None")}" '
                    "but expecting one of "
                    f'"{self._params.retry_data[ATTR_EXPECTED_STATE]}"'
                )
                raise InvalidStateError(message)
            if not self._check_validation():
                message = (
                    f'"{self._params.retry_data[ATTR_VALIDATION].template}" is False'
                )
                raise InvalidStateError(message)

    def _check_state(self, entity: Entity | None) -> bool:
        """Check if the entity's state is expected."""
        if not entity or ATTR_EXPECTED_STATE not in self._params.retry_data:
            return True
        for expected in self._params.retry_data[ATTR_EXPECTED_STATE]:
            if entity.state == expected:
                return True
            try:
                if float(entity.state) == float(expected):  # type: ignore[reportArgumentType]
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

    @property
    def _action_str(self) -> str:
        if self._action_str_value is None:
            self._action_str_value = self._compose_action_str()
        return self._action_str_value

    def _compose_action_str(self) -> str:
        """Return a string with the service call parameters."""
        service_call = (
            f"{self._params.retry_data[ATTR_DOMAIN]}."
            f"{self._params.retry_data[ATTR_SERVICE]}"
            f"({', '.join(
                [f'{key}={value}' for key, value in self._inner_data.items()]
            )})"
        )
        retry_params = []
        if (
            expected_state := self._params.retry_data.get(ATTR_EXPECTED_STATE)
        ) is not None:
            if len(expected_state) == 1:
                retry_params.append(f"expected_state={expected_state[0]}")
            else:
                retry_params.append(
                    f"expected_state in ({', '.join(
                        state for state in expected_state
                    )})"
                )
        for name, value, default in (
            (
                ATTR_BACKOFF,
                self._params.retry_data[ATTR_BACKOFF].template,
                DEFAULT_BACKOFF_FIXED,
            ),
            (
                ATTR_VALIDATION,
                self._params.retry_data[ATTR_VALIDATION].template
                if ATTR_VALIDATION in self._params.retry_data
                else None,
                None,
            ),
            (ATTR_STATE_DELAY, self._params.retry_data[ATTR_STATE_DELAY], 0),
            (
                ATTR_STATE_GRACE,
                self._params.retry_data[ATTR_STATE_GRACE],
                DEFAULT_STATE_GRACE,
            ),
            (
                ATTR_RETRY_ID,
                self._params.retry_data.get(ATTR_RETRY_ID),
                None if ATTR_RETRY_ID not in self._params.retry_data else "add",
            ),
        ):
            if value != default:
                if isinstance(value, str):
                    retry_params.append(f'{name}="{value}"')
                else:
                    retry_params.append(f"{name}={value}")
        if len(retry_params) > 0:
            service_call += f"[{', '.join(retry_params)}]"
        return service_call

    def _log(self, level: int, prefix: str, stack_info: bool = False) -> None:  # noqa: FBT001, FBT002
        """Log entry."""
        LOGGER.log(
            level,
            "[%s]: attempt %d/%d: %s",
            prefix,
            self._attempt,
            self._params.retry_data[ATTR_RETRIES],
            self._action_str,
            exc_info=stack_info,
        )

    def _repair(self) -> None:
        """Create a repair ticket."""
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self._action_str,
            is_fixable=False,
            learn_more_url="https://github.com/amitfin/retry#retryaction",
            severity=ir.IssueSeverity.ERROR,
            translation_key="failure",
            translation_placeholders={
                "action": self._action_str,
                "retries": self._params.retry_data[ATTR_RETRIES],
            },
        )

    def _start_id(self) -> None:
        """Add or override self as the retry ID running job."""
        if not self._retry_id:
            return
        with _running_retries_write_lock:
            self._set_id(
                1 if not self._check_id() else _running_retries[self._retry_id][1] + 1
            )

    def _end_id(self) -> None:
        """Remove self from being the retry ID running job."""
        if not self._retry_id:
            return
        with _running_retries_write_lock:
            if not self._check_id():
                return
            count = _running_retries[self._retry_id][1] - 1
            if not count:
                del _running_retries[self._retry_id]
            else:
                self._set_id(count)

    def _set_id(self, count: int) -> None:
        """Set the retry_id entry with a counter."""
        if self._retry_id:
            _running_retries[self._retry_id] = (self._context.id, count)

    def _check_id(self) -> bool:
        """Check if self is the retry ID running job."""
        return (
            not self._retry_id
            or _running_retries.get(self._retry_id, [None])[0] == self._context.id
        )

    @callback
    async def async_retry(self, _: datetime.datetime | None = None) -> None:
        """One attempt."""
        if not self._check_id():
            self._log(logging.INFO, "Cancelled")
            return
        try:
            await self._hass.services.async_call(
                self._params.retry_data[ATTR_DOMAIN],
                self._params.retry_data[ATTR_SERVICE],
                self._inner_data.copy(),
                blocking=True,
                context=Context(self._context.user_id, self._context.id),
            )
            await self._async_validate()
            self._log(
                logging.DEBUG if self._attempt == 1 else logging.INFO, "Succeeded"
            )
            self._end_id()
        except Exception:  # noqa: BLE001
            self._log(
                logging.WARNING
                if self._attempt < self._params.retry_data[ATTR_RETRIES]
                else logging.ERROR,
                "Failed",
                stack_info=True,
            )
            if self._attempt == self._params.retry_data[ATTR_RETRIES]:
                if not getattr(self._params.config_entry, "options", {}).get(
                    CONF_DISABLE_REPAIR
                ):
                    self._repair()
                self._end_id()
                if (on_error := self._params.retry_data.get(ATTR_ON_ERROR)) is not None:
                    await script.Script(
                        self._hass, on_error, ACTION_SERVICE, DOMAIN
                    ).async_run(
                        run_variables={ATTR_ENTITY_ID: self._entity_id}
                        if self._entity_id
                        else None,
                        context=Context(self._context.user_id, self._context.id),
                    )
                return
            next_retry = dt_util.now() + datetime.timedelta(
                seconds=float(
                    self._params.retry_data[ATTR_BACKOFF].async_render(
                        variables={"attempt": self._attempt - 1}
                    )
                )
            )
            self._attempt += 1
            event.async_track_point_in_time(self._hass, self.async_retry, next_retry)


def _wrap_actions(  # noqa: PLR0912
    hass: HomeAssistant, sequence: list[dict], retry_params: dict[str, Any]
) -> None:
    """Warp any action with retry."""
    for action in sequence:
        action_type = cv.determine_script_action(action)
        match action_type:
            case cv.SCRIPT_ACTION_CALL_SERVICE:
                domain_service = (
                    action[CONF_ACTION]
                    if CONF_ACTION in action
                    else action[ATTR_SERVICE]
                )
                if domain_service == f"{DOMAIN}.{ACTIONS_SERVICE}":
                    message = "Nested retry.actions are disallowed"
                    raise IntegrationError(message)
                if domain_service in [
                    f"{DOMAIN}.{ACTION_SERVICE}",
                    f"{DOMAIN}.{CALL_SERVICE}",
                ]:
                    message = f"{domain_service} inside retry.actions is disallowed"
                    raise IntegrationError(message)
                action[ATTR_DATA] = action.get(ATTR_DATA, {})
                action[ATTR_DATA][CONF_ACTION] = domain_service
                action[ATTR_DATA].update(retry_params)
                action[CONF_ACTION] = f"{DOMAIN}.{ACTION_SERVICE}"
                # Validate parameters so errors are not raised in the background.
                RetryParams(
                    hass,
                    None,
                    {**action[ATTR_DATA], **action.get(CONF_TARGET, {})},
                )
            case cv.SCRIPT_ACTION_REPEAT:
                _wrap_actions(hass, action[CONF_REPEAT][CONF_SEQUENCE], retry_params)
            case cv.SCRIPT_ACTION_CHOOSE:
                for choose in action[CONF_CHOOSE]:
                    _wrap_actions(hass, choose[CONF_SEQUENCE], retry_params)
                if CONF_DEFAULT in action:
                    _wrap_actions(hass, action[CONF_DEFAULT], retry_params)
            case cv.SCRIPT_ACTION_IF:
                _wrap_actions(hass, action[CONF_THEN], retry_params)
                if CONF_ELSE in action:
                    _wrap_actions(hass, action[CONF_ELSE], retry_params)
            case cv.SCRIPT_ACTION_PARALLEL:
                for parallel in action[CONF_PARALLEL]:
                    _wrap_actions(hass, parallel[CONF_SEQUENCE], retry_params)
            case cv.SCRIPT_ACTION_SEQUENCE:
                _wrap_actions(hass, action[CONF_SEQUENCE], retry_params)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up domain."""

    async def async_action(service_call: ServiceCall) -> None:
        """Execute action with background retries."""
        params = RetryParams(hass, config_entry, service_call.data)
        for entity_id in params.entities or [None]:
            hass.async_create_task(
                RetryAction(hass, params, service_call.context, entity_id).async_retry()
            )

    hass.services.async_register(
        DOMAIN, ACTION_SERVICE, async_action, ACTION_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, CALL_SERVICE, async_action, ACTION_SERVICE_SCHEMA
    )

    async def async_actions(service_call: ServiceCall) -> None:
        """Execute actions and retry failed actions."""
        sequence = service_call.data[CONF_SEQUENCE].copy()
        retry_params: dict[str, Any] = {
            key: service_call.data[key]
            for key in service_call.data
            if key in SERVICE_SCHEMA_BASE_FIELDS
        }
        for key in [ATTR_BACKOFF, ATTR_VALIDATION]:
            if key in retry_params:
                # Revert it back to string so it won't get rendered in advance.
                retry_params[key] = retry_params[key].template
        _wrap_actions(hass, sequence, retry_params)
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
    hass.services.async_remove(DOMAIN, ACTION_SERVICE)
    hass.services.async_remove(DOMAIN, CALL_SERVICE)
    hass.services.async_remove(DOMAIN, ACTIONS_SERVICE)
    return True
