"""Constants for the retry integration."""

import logging
from typing import Final

DOMAIN: Final = "retry"
LOGGER = logging.getLogger(__package__)

ACTION_SERVICE: Final = "action"
ACTIONS_SERVICE: Final = "actions"
CALL_SERVICE: Final = "call"
CONF_DISABLE_REPAIR = "disable_repair"
ATTR_BACKOFF: Final = "backoff"
ATTR_EXPECTED_STATE: Final = "expected_state"
ATTR_ON_ERROR: Final = "on_error"
ATTR_RETRY_ID: Final = "retry_id"
ATTR_RETRIES: Final = "retries"
ATTR_STATE_DELAY: Final = "state_delay"
ATTR_STATE_GRACE: Final = "state_grace"
ATTR_VALIDATION: Final = "validation"
