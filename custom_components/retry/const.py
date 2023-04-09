"""Constants for the retry integration."""
import logging
from typing import Final

DOMAIN: Final = "retry"
LOGGER = logging.getLogger(__package__)

SERVICE: Final = "call"
ATTR_RETRIES: Final = "retries"
ATTR_EXPECTED_STATE: Final = "expected_state"
