"""Constants for the retry integration."""

import logging
from typing import Final

DOMAIN: Final = "retry"
LOGGER = logging.getLogger(__package__)

ACTION_SERVICE: Final = "action"
ACTIONS_SERVICE: Final = "actions"
CONF_DISABLE_REPAIR = "disable_repair"
CONF_DISABLE_INITIAL_CHECK = "disable_initial_check"
ATTR_BACKOFF: Final = "backoff"
ATTR_EXPECTED_STATE: Final = "expected_state"
ATTR_IGNORE_TARGET: Final = "ignore_target"
ATTR_ON_ERROR: Final = "on_error"
ATTR_REPAIR: Final = "repair"
ATTR_RETRY_ID: Final = "retry_id"
ATTR_RETRIES: Final = "retries"
ATTR_STATE_DELAY: Final = "state_delay"
ATTR_STATE_GRACE: Final = "state_grace"
ATTR_VALIDATION: Final = "validation"
ATTEMPT_VARIABLE: Final = "attempt"
