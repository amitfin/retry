"""Retry integration."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import logging
import threading
from typing import TYPE_CHECKING, Any, cast

import jinja2
import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_SERVICE,
    CONF_ACTION,
    CONF_CHOOSE,
    CONF_DEFAULT,
    CONF_ELSE,
    CONF_ENABLED,
    CONF_PARALLEL,
    CONF_REPEAT,
    CONF_SEQUENCE,
    CONF_SERVICE_DATA,
    CONF_SERVICE_DATA_TEMPLATE,
    CONF_SERVICE_TEMPLATE,
    CONF_TARGET,
    CONF_THEN,
    ENTITY_MATCH_ALL,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import (
    IntegrationError,
    InvalidStateError,
    ServiceNotFound,
    ServiceValidationError,
    TemplateError,
)
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    issue_registry as ir,
)
from homeassistant.helpers import script
from homeassistant.helpers.entity_component import DATA_INSTANCES, EntityComponent
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.target import async_extract_referenced_entity_ids
from homeassistant.helpers.template import Template, is_complex, result_as_boolean

try:
    from homeassistant.helpers.target import TargetSelection
except ImportError:
    from homeassistant.helpers.target import TargetSelectorData as TargetSelection

if TYPE_CHECKING:
    from homeassistant.core import Context, HomeAssistant, ServiceCall
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.typing import ConfigType, VolSchemaType

from .const import (
    ACTION_SERVICE,
    ACTIONS_SERVICE,
    ATTEMPT_VARIABLE,
    ATTR_BACKOFF,
    ATTR_EXPECTED_STATE,
    ATTR_IGNORE_TARGET,
    ATTR_INNER_DATA,
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
    LOGGER,
)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

DEFAULT_BACKOFF = f"{{{{ 2 ** {ATTEMPT_VARIABLE} }}}}"
DEFAULT_RETRIES = 7
DEFAULT_STATE_GRACE = 0.2
GROUP_DOMAIN = "group"
RETURN_RESPONSE = "return_response"
ENTITY_SERVICE_FIELDS = {str(key) for key in cv.ENTITY_SERVICE_FIELDS}

_NOT_SET = object()  # A parameter which isn't provided (None is a valid value).

_running_retries: dict[str, tuple[str, int]] = {}
_running_retries_write_lock = threading.Lock()


def _template_parameter(value: Any) -> str:
    """Render template parameter."""
    return str(cv.template(value).async_render(parse_result=False))


_ARTIFICIAL_TOKENS = {
    "[[": "{{",
    "]]": "}}",
    "[%": "{%",
    "%]": "%}",
    "[#": "{#",
    "#]": "#}",
}
# Used only for lexing templates with the artificial tokens (nothing is rendered).
_ARTIFICIAL_TOKENS_ENV = jinja2.Environment(  # noqa: S701
    variable_start_string="[[",
    variable_end_string="]]",
    block_start_string="[%",
    block_end_string="%]",
    comment_start_string="[#",
    comment_end_string="#]",
    keep_trailing_newline=True,
)
_DELIMITER_TOKEN_TYPES = {
    "variable_begin",
    "variable_end",
    "block_begin",
    "block_end",
    "raw_begin",
    "raw_end",
    "comment_begin",
    "comment_end",
}


def _fix_template_tokens(value: str) -> str:
    """Replace template's artificial tokens brackets with Jinja's valid tokens."""
    # Jinja's lexer finds the actual delimiters, so brackets inside the template
    # (e.g. a nested list or a string literal) aren't replaced.
    result = []
    try:
        for _, token_type, token in _ARTIFICIAL_TOKENS_ENV.lex(value):
            if token_type in _DELIMITER_TOKEN_TYPES:
                for artificial, valid in _ARTIFICIAL_TOKENS.items():
                    token = token.replace(artificial, valid)  # noqa: PLW2901
            result.append(token)
    except jinja2.TemplateSyntaxError as error:
        message = f"invalid template ({error.message})"
        raise vol.Invalid(message) from error
    return "".join(result)


def _backoff_parameter(value: Any) -> str:
    """Check backoff parameter."""
    value_str = cv.string(value)
    vol.Length(min=1)(cv.template(_fix_template_tokens(value_str)).template)
    return value_str


def _validation_parameter(value: Any) -> str:
    """Check validation parameter."""
    value_str = cv.string(value)
    cv.dynamic_template(_fix_template_tokens(value_str))
    return value_str


def _script_schema_validate_only(value: Any) -> Any:
    """Validate script schema without changing the value."""
    cv.SCRIPT_SCHEMA(copy.deepcopy(value))
    return value


def _expected_state_without_ignore_target(value: dict[str, Any]) -> dict[str, Any]:
    """Check expected_state isn't used together with ignore_target (if true)."""
    if ATTR_EXPECTED_STATE in value and value.get(ATTR_IGNORE_TARGET):
        message = f"{ATTR_EXPECTED_STATE} can't be used with {ATTR_IGNORE_TARGET}"
        raise vol.Invalid(message)
    return value


SERVICE_SCHEMA_BASE_FIELDS = {
    vol.Required(ATTR_RETRIES, default=DEFAULT_RETRIES): vol.All(
        cv.positive_int, vol.Range(min=1)
    ),
    vol.Required(ATTR_BACKOFF, default=DEFAULT_BACKOFF): _backoff_parameter,
    vol.Optional(ATTR_EXPECTED_STATE): vol.All(cv.ensure_list, [_template_parameter]),
    vol.Optional(ATTR_VALIDATION): _validation_parameter,
    vol.Required(ATTR_STATE_DELAY, default=0): cv.positive_float,
    vol.Required(ATTR_STATE_GRACE, default=DEFAULT_STATE_GRACE): cv.positive_float,
    vol.Optional(ATTR_ON_ERROR): cv.SCRIPT_SCHEMA,
    vol.Optional(ATTR_IGNORE_TARGET): cv.boolean,
    vol.Optional(ATTR_REPAIR): cv.boolean,
    vol.Optional(ATTR_RETRY_ID): vol.Any(cv.string, None),
}
ACTION_SERVICE_PARAMS = vol.Schema(
    {
        **SERVICE_SCHEMA_BASE_FIELDS,
        vol.Required(CONF_ACTION): vol.All(_template_parameter, cv.service),
        vol.Optional(ATTR_INNER_DATA): dict,
    },
    extra=vol.ALLOW_EXTRA,
)
# HA 2026.9+ annotates schemas with probatio types, while mypy sees voluptuous.
ACTION_SERVICE_SCHEMA = cast(
    "VolSchemaType",
    vol.All(ACTION_SERVICE_PARAMS, _expected_state_without_ignore_target),
)

ACTIONS_SERVICE_SCHEMA = cast(
    "VolSchemaType",
    vol.All(
        vol.Schema(
            {
                **SERVICE_SCHEMA_BASE_FIELDS,
                vol.Required(CONF_SEQUENCE): cv.SCRIPT_SCHEMA,
                # "on_error" should be passed as-is to retry.action
                vol.Optional(ATTR_ON_ERROR): _script_schema_validate_only,
                # The frontend stores data here (like HA's own schemas).
                vol.Remove("metadata"): dict,
            },
        ),
        _expected_state_without_ignore_target,
    ),
)


def _get_entity_component(hass: HomeAssistant, domain: str) -> EntityComponent | None:
    """Get entity component object."""
    return hass.data.get(DATA_INSTANCES, {}).get(domain)


def _entity_state(entity: Entity) -> Any:
    """Return the entity's state as the state machine shows it (None is unknown)."""
    return STATE_UNKNOWN if entity.state is None else entity.state


def _get_entity(hass: HomeAssistant, entity_id: str) -> Entity | None:
    """Get entity object."""
    entity_comp = _get_entity_component(hass, entity_id.split(".", maxsplit=1)[0])
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
        self.config_options = getattr(config_entry, "options", {})
        self.retry_data = self._retry_data(hass, data)
        self.inner_data = self._inner_data(hass, data)
        self.has_target = self._has_target()
        self.entities = self._entity_ids(hass)
        if not self.has_target and ATTR_EXPECTED_STATE in self.retry_data:
            message = f"{ATTR_EXPECTED_STATE} parameter requires an entity"
            raise ServiceValidationError(message)

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
        retry_data[RETURN_RESPONSE] = (
            hass.services.supports_response(domain, service) != SupportsResponse.NONE
        )
        for key in [ATTR_BACKOFF, ATTR_VALIDATION]:
            if key in retry_data:
                retry_data[key] = Template(_fix_template_tokens(retry_data[key]), hass)
        return retry_data

    def _inner_data(self, hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
        """Compose inner action parameters."""
        inner_data = {
            key: value
            for key, value in data.items()
            if key not in ACTION_SERVICE_PARAMS.schema
        }
        # inner_data allows parameters which collide with retry's own parameters.
        nested_data = data.get(ATTR_INNER_DATA, {})
        if duplicates := inner_data.keys() & nested_data.keys():
            message = (
                f"{', '.join(sorted(duplicates))} provided both in "
                f"{ATTR_INNER_DATA} and outside of it"
            )
            raise ServiceValidationError(message)
        inner_data.update(nested_data)
        domain_services = hass.services.async_services_for_domain(
            self.retry_data[ATTR_DOMAIN]
        )
        if schema := domain_services[self.retry_data[ATTR_SERVICE]].schema:
            schema(inner_data)
        return inner_data

    def _has_target(self) -> bool:
        """Check if inner action refers to entities."""
        if self.retry_data.get(ATTR_IGNORE_TARGET):
            return False
        return self.inner_data.keys() & ENTITY_SERVICE_FIELDS != set()

    def _expand_group(
        self, hass: HomeAssistant, entity_id: str, visited: set[str] | None = None
    ) -> set[str]:
        """Return group member ids (when a group)."""
        visited = visited if visited is not None else set()
        if entity_id in visited:
            return set()  # Groups which contain each other.
        visited.add(entity_id)
        entity_ids = set()
        entity_obj = _get_entity(hass, entity_id)
        if (
            entity_obj is not None
            and entity_obj.platform is not None
            and entity_obj.platform.platform_name == GROUP_DOMAIN
        ):
            for member_id in getattr(entity_obj, "extra_state_attributes", {}).get(
                ATTR_ENTITY_ID, []
            ):
                entity_ids.update(self._expand_group(hass, member_id, visited))
        else:
            entity_ids.add(entity_id)
        return entity_ids

    def _all_entity_ids(self, hass: HomeAssistant) -> set[str]:
        """Return all entity ids based on action's domain."""
        # 1) All entities with the same domain as the action.
        # 2) All entities created by the integration of the action's domain.
        # Note that it's not possible to know the specific platform based on
        # the action name, so we can't filter to a specific platform.
        return {
            entity.entity_id
            for entity in getattr(
                _get_entity_component(hass, self.retry_data[ATTR_DOMAIN]),
                "entities",
                [],
            )
        } | {
            entity_id
            for platform in async_get_platforms(hass, self.retry_data[ATTR_DOMAIN])
            for entity_id in platform.entities
        }

    def _entity_ids(self, hass: HomeAssistant) -> set[str]:
        """Extract and expand entity ids."""
        if not self.has_target:
            return set()

        target = self.inner_data
        if isinstance(raw_entity_ids := target.get(ATTR_ENTITY_ID), str):
            # Normalize like HA's entity services, e.g. "light.a, light.b" or "ALL".
            # Strings which entity services reject are used as-is.
            with contextlib.suppress(vol.Invalid):
                target = {**target, ATTR_ENTITY_ID: cv.comp_entity_ids(raw_entity_ids)}

        if target.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL:
            return self._all_entity_ids(hass)

        entities = async_extract_referenced_entity_ids(
            hass,
            TargetSelection(target),
        )

        entity_ids = {
            entity_id
            for group_entity_id in entities.referenced
            for entity_id in self._expand_group(hass, group_entity_id)
        }

        if entities.indirectly_referenced:
            all_entity_ids = self._all_entity_ids(hass)
            entity_ids.update(
                entity_id
                for group_entity_id in entities.indirectly_referenced
                for entity_id in self._expand_group(hass, group_entity_id)
                if entity_id in all_entity_ids
                or self.retry_data[ATTR_DOMAIN] == HA_DOMAIN  # homeassistant.turn_on
            )

        return entity_ids


class RetryAction:
    """Perform an action with retries on failures."""

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
        self._action = (
            f"{params.retry_data[ATTR_DOMAIN]}.{params.retry_data[ATTR_SERVICE]}"
        )
        self._inner_data = params.inner_data.copy()
        if entity_id:
            self._inner_data = {
                ATTR_ENTITY_ID: entity_id,
                **{
                    key: value
                    for key, value in self._inner_data.items()
                    if key not in ENTITY_SERVICE_FIELDS
                },
            }
        self._entity_id = entity_id
        self._context = context
        self._attempt = 1
        self._template_variables = {
            **self._inner_data,
            # Retry's own variables take precedence over the inner action's data.
            CONF_ACTION: self._action,
            ATTEMPT_VARIABLE: 0,
        }
        # Rendered (and validated) in advance: the wait after each failed attempt.
        self._backoffs = [
            cv.positive_float(
                self._params.retry_data[ATTR_BACKOFF].async_render(
                    variables={**self._template_variables, ATTEMPT_VARIABLE: attempt}
                )
            )
            for attempt in range(self._params.retry_data[ATTR_RETRIES] - 1)
        ]
        self._retry_id = params.retry_data.get(
            ATTR_RETRY_ID, self._entity_id or self._action
        )
        self._str_cache: str | None = None
        self._validation_error: TemplateError | None = None

    def _get_template_variables(self) -> dict[str, Any]:
        """Return template variables."""
        self._template_variables[ATTEMPT_VARIABLE] = self._attempt - 1
        return self._template_variables

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
        if (state_delay := self._params.retry_data[ATTR_STATE_DELAY]) > 0 and (
            ATTR_EXPECTED_STATE in self._params.retry_data
            or ATTR_VALIDATION in self._params.retry_data
        ):
            await asyncio.sleep(state_delay)
        if not self._check_state(ent_obj) or not self._check_validation():
            await asyncio.sleep(self._params.retry_data[ATTR_STATE_GRACE])
            if not self._check_state(ent_obj):
                state = _entity_state(ent_obj) if ent_obj else None
                message = (
                    f'{self._entity_id} state is "{state}" '
                    "but expecting one of "
                    f'"{self._params.retry_data[ATTR_EXPECTED_STATE]}"'
                )
                raise InvalidStateError(message)
            if not self._check_validation():
                message = (
                    f'"{self._params.retry_data[ATTR_VALIDATION].template}" is False'
                )
                if self._validation_error:
                    message += f" ({self._validation_error})"
                raise InvalidStateError(message)

    def _check_state(self, entity: Entity | None) -> bool:
        """Check if the entity's state is expected."""
        if not entity or ATTR_EXPECTED_STATE not in self._params.retry_data:
            return True
        state = _entity_state(entity)
        for expected in self._params.retry_data[ATTR_EXPECTED_STATE]:
            if state == expected:
                return True
            try:
                if float(state) == float(expected):
                    return True
            except ValueError:
                pass
        return False

    def _check_validation(self) -> bool:
        """Check if the validation statement is true."""
        if ATTR_VALIDATION not in self._params.retry_data:
            return True
        self._validation_error = None
        try:
            return result_as_boolean(
                self._params.retry_data[ATTR_VALIDATION].async_render(
                    variables=self._get_template_variables(),
                )
            )
        except TemplateError as error:
            # E.g. an attribute which doesn't exist yet. It's not satisfied.
            self._validation_error = error
            return False

    def _initial_check(self) -> bool:
        """Check if the state is already as expected and/or the validation passes."""
        if self._params.config_options.get(CONF_DISABLE_INITIAL_CHECK):
            return False

        result = False

        if ATTR_EXPECTED_STATE in self._params.retry_data and self._entity_id:
            if (
                (ent_obj := _get_entity(self._hass, self._entity_id)) is None
                or not ent_obj.available
                or not self._check_state(ent_obj)
            ):
                return False
            result = True

        if ATTR_VALIDATION in self._params.retry_data:
            if not self._check_validation():
                return False
            result = True

        return result

    def __str__(self) -> str:
        """Return a string representation of the object."""
        if self._str_cache is None:
            self._str_cache = self._compose_str()
        return self._str_cache

    def _compose_str(self) -> str:
        """Compose a string representation of the object."""
        str_value = (
            self._action
            + f"({
                ', '.join([f'{key}={value}' for key, value in self._inner_data.items()])
            })"
        )
        retry_params = []
        if (
            expected_state := self._params.retry_data.get(ATTR_EXPECTED_STATE)
        ) is not None:
            if len(expected_state) == 1:
                retry_params.append(f"expected_state={expected_state[0]}")
            else:
                retry_params.append(
                    f"expected_state in ({
                        ', '.join(state for state in expected_state)
                    })"
                )
        for name, value, default in (
            (
                ATTR_BACKOFF,
                self._params.retry_data[ATTR_BACKOFF].template,
                DEFAULT_BACKOFF,
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
                ATTR_IGNORE_TARGET,
                self._params.retry_data.get(ATTR_IGNORE_TARGET, False),
                False,
            ),
            (
                ATTR_RETRY_ID,
                self._params.retry_data.get(ATTR_RETRY_ID, _NOT_SET),
                _NOT_SET,
            ),
        ):
            if value != default:
                if isinstance(value, str):
                    retry_params.append(f'{name}="{value}"')
                else:
                    retry_params.append(f"{name}={value}")
        if len(retry_params) > 0:
            str_value += f"[{', '.join(retry_params)}]"
        return str_value

    def _log(self, level: int, prefix: str, exc_info: bool = False) -> None:  # noqa: FBT001, FBT002
        """Log entry."""
        LOGGER.log(
            level,
            "[%s]: attempt %d/%d: %s",
            prefix,
            self._attempt,
            self._params.retry_data[ATTR_RETRIES],
            str(self),
            exc_info=exc_info,
        )

    def _repair(self) -> None:
        """Create a repair ticket."""
        # A short ID, which is the same for identical actions (de-dup).
        issue_id = hashlib.sha256(str(self).encode()).hexdigest()
        ir.async_delete_issue(self._hass, DOMAIN, issue_id)
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            # Kept until the user marks it as resolved (HA's ConfirmRepairFlow).
            is_fixable=True,
            is_persistent=True,
            learn_more_url="https://github.com/amitfin/retry#retryaction",
            severity=ir.IssueSeverity.ERROR,
            translation_key="failure",
            translation_placeholders={
                "action": str(self),
                "retries": str(self._params.retry_data[ATTR_RETRIES]),
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
            if self._check_id():
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

    async def async_retry(self) -> Any:
        """Perform the attempts, while owning the retry ID."""
        self._start_id()
        try:
            return await self._async_attempts()
        finally:
            self._end_id()

    async def _async_attempts(self) -> Any:
        """Loop of attempts."""
        result = None
        while True:
            if not self._check_id():
                self._log(logging.INFO, "Cancelled")
                msg = (
                    f"Retry cancelled due to duplicate retry_id '{self._retry_id}': "
                    f"{self!s}"
                )
                raise IntegrationError(msg)
            try:
                if not self._initial_check():
                    result = await self._hass.services.async_call(
                        self._params.retry_data[ATTR_DOMAIN],
                        self._params.retry_data[ATTR_SERVICE],
                        self._inner_data.copy(),
                        blocking=True,
                        context=self._context,
                        return_response=self._params.retry_data[RETURN_RESPONSE],
                    )
                    await self._async_validate()
            except Exception:
                self._log(
                    logging.WARNING
                    if self._attempt < self._params.retry_data[ATTR_RETRIES]
                    else logging.ERROR,
                    "Failed",
                    exc_info=True,
                )
                if self._attempt >= self._params.retry_data[ATTR_RETRIES]:
                    issue_repair = self._params.retry_data.get(ATTR_REPAIR)
                    if issue_repair is None:
                        issue_repair = not self._params.config_options.get(
                            CONF_DISABLE_REPAIR
                        )
                    if issue_repair:
                        self._repair()
                    if (
                        on_error := self._params.retry_data.get(ATTR_ON_ERROR)
                    ) is not None:
                        # The script logs its own errors. The original error is
                        # the one which is raised.
                        with contextlib.suppress(Exception):
                            await _async_run_script(
                                self._hass,
                                on_error,
                                ACTION_SERVICE,
                                self._context,
                                {
                                    key: value
                                    for key, value in self._template_variables.items()
                                    if key != ATTEMPT_VARIABLE
                                },
                            )
                    raise
                await asyncio.sleep(self._backoffs[self._attempt - 1])
                self._attempt += 1
            else:
                self._log(
                    logging.DEBUG if self._attempt == 1 else logging.INFO, "Succeeded"
                )
                return result


async def _async_run_script(
    hass: HomeAssistant,
    sequence: list[dict[str, Any]],
    name: str,
    context: Context,
    run_variables: dict[str, Any] | None = None,
) -> None:
    """Run an ad-hoc script and unload it afterwards."""
    script_obj = script.Script(hass, sequence, name, DOMAIN)
    try:
        await script_obj.async_run(run_variables=run_variables, context=context)
    finally:
        # HA keeps a reference to every script until it's unloaded. HA 2026.4 and
        # older have no way to unload a script, so the script is kept there.
        if hasattr(script_obj, "async_unload"):
            await script_obj.async_unload()


def _step_data(action: dict[str, Any]) -> Any:
    """Remove and return a step's data, merged like HA does (the target wins)."""
    data = action.get(CONF_SERVICE_DATA)
    data_template = action.get(CONF_SERVICE_DATA_TEMPLATE)
    if data is not None and data_template is not None:
        if not isinstance(data, dict) or not isinstance(data_template, dict):
            # A template of the whole data can be merged only once it's rendered,
            # so data_template stays a parameter of the step (rare legacy syntax).
            return action.pop(CONF_SERVICE_DATA)
        merged: Any = {**data, **data_template}
    else:
        merged = data if data is not None else data_template
    action.pop(CONF_SERVICE_DATA, None)
    action.pop(CONF_SERVICE_DATA_TEMPLATE, None)
    if isinstance(merged, dict):
        target = action.get(CONF_TARGET)
        target_keys = set(target) if isinstance(target, dict) else set()
        if ATTR_ENTITY_ID in action:  # Legacy syntax (outside of the target).
            target_keys.add(ATTR_ENTITY_ID)
        merged = {key: value for key, value in merged.items() if key not in target_keys}
    return merged


def _wrap_actions(  # noqa: PLR0912
    hass: HomeAssistant, sequence: list[dict[str, Any]], retry_params: dict[str, Any]
) -> None:
    """Warp any action with retry."""
    for action in sequence:
        if action.get(CONF_ENABLED) is False:
            continue  # Never performed (a template is checked when it runs).
        action_type = cv.determine_script_action(action)
        match action_type:
            case cv.SCRIPT_ACTION_CALL_SERVICE:
                domain_service = (
                    action.pop(CONF_SERVICE_TEMPLATE)  # Legacy syntax.
                    if CONF_SERVICE_TEMPLATE in action
                    else action[CONF_ACTION]
                )
                if domain_service == f"{DOMAIN}.{ACTIONS_SERVICE}":
                    message = "Nested retry.actions are disallowed"
                    raise ServiceValidationError(message)
                if domain_service == f"{DOMAIN}.{ACTION_SERVICE}":
                    message = "retry.action inside retry.actions is disallowed"
                    raise ServiceValidationError(message)
                # The step's data is passed separately, so it can't collide with
                # the retry parameters (e.g. a field named "action").
                step_data = _step_data(action)
                action[CONF_SERVICE_DATA] = {
                    CONF_ACTION: domain_service,
                    **copy.deepcopy(retry_params),
                    **({ATTR_INNER_DATA: step_data} if step_data else {}),
                }
                action[CONF_ACTION] = f"{DOMAIN}.{ACTION_SERVICE}"
                target = action.get(CONF_TARGET, {})
                call_data = {
                    **action[CONF_SERVICE_DATA],
                    **(target if isinstance(target, dict) else {}),
                    # Legacy syntax (outside of the target).
                    **(
                        {ATTR_ENTITY_ID: action[ATTR_ENTITY_ID]}
                        if ATTR_ENTITY_ID in action
                        else {}
                    ),
                }
                # Validate parameters so errors are raised as soon as possible.
                # Templates are rendered only when the step runs (like HA does).
                # A missing action fails only if its step runs (like HA does),
                # e.g. in a branch which isn't taken.
                if isinstance(target, dict) and not is_complex(call_data):
                    with contextlib.suppress(ServiceNotFound):
                        RetryParams(hass, None, call_data)
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


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Set up integration."""
    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}
            )
        )

    def get_config_entry() -> ConfigEntry:
        """Get integration's config first (and only) entry."""
        config_entries = hass.config_entries.async_entries(DOMAIN)
        if not config_entries:
            message = "Config entry not found"
            raise ServiceValidationError(message)
        if config_entries[0].state is not ConfigEntryState.LOADED:
            message = "Config entry not loaded"
            raise ServiceValidationError(message)
        return config_entries[0]

    async def async_action(service_call: ServiceCall) -> Any:
        """Perform action with retries."""
        data: dict[str, Any] = service_call.data
        if (on_error := data.get(ATTR_ON_ERROR)) is not None:
            # Full validation, like automations (e.g. device actions).
            data = {
                **data,
                ATTR_ON_ERROR: await script.async_validate_actions_config(
                    hass, on_error
                ),
            }
        params = RetryParams(hass, get_config_entry(), data)

        # All loops are created first: if one fails, none of them runs.
        retry_actions = [
            RetryAction(hass, params, service_call.context, entity_id)
            for entity_id in (params.entities if params.has_target else [None])
        ]
        results = await asyncio.gather(
            *[retry_action.async_retry() for retry_action in retry_actions],
            return_exceptions=True,
        )

        if error := next(
            (result for result in results if isinstance(result, BaseException)),
            None,
        ):
            raise error

        return next((result for result in results if result is not None), {})

    hass.services.async_register(
        DOMAIN,
        ACTION_SERVICE,
        async_action,
        ACTION_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def async_actions(service_call: ServiceCall) -> None:
        """Perform actions and retry failed actions."""
        # Full validation, like automations (e.g. device actions).
        sequence = await script.async_validate_actions_config(
            hass, service_call.data[CONF_SEQUENCE]
        )
        retry_params: dict[str, Any] = {
            key: service_call.data[key]
            for key in service_call.data
            if key in SERVICE_SCHEMA_BASE_FIELDS
        }
        _wrap_actions(hass, sequence, retry_params)
        await _async_run_script(hass, sequence, ACTIONS_SERVICE, service_call.context)

    hass.services.async_register(
        DOMAIN,
        ACTIONS_SERVICE,
        async_actions,
        ACTIONS_SERVICE_SCHEMA,
    )

    return True


async def async_setup_entry(_hass: HomeAssistant, _config_entry: ConfigEntry) -> bool:
    """Set up config entry."""
    return True


async def async_unload_entry(_hass: HomeAssistant, _config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return True
