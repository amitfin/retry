"""Constants for the retry integration."""
import logging
from typing import Final

DOMAIN: Final = "retry"
LOGGER = logging.getLogger(__package__)

SERVICE: Final = "call"
RETRY_SERVICE: Final = "retry_service"
RETRIES: Final = "retries"
