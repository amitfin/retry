"""Global fixtures for retry integration."""

# Fixtures allow you to replace functions with a Mock object. You can perform
# many options via the Mock to reflect a particular behavior from the original
# function that you want to see without going through the function's actual logic.
# Fixtures can either be passed into tests as parameters, or if autouse=True, they
# will automatically be used across all tests.
#
# Fixtures that are defined in conftest.py are available across all tests. You can also
# define fixtures within a particular test file to scope them locally.
#
# pytest_homeassistant_custom_component provides some fixtures that are provided by
# Home Assistant core. You can find those fixture definitions here:
# https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/blob/master/pytest_homeassistant_custom_component/common.py
#
# See here for more info: https://docs.pytest.org/en/latest/fixture.html (note that
# pytest includes fixtures OOB which you can use as defined on this page)
import logging
from itertools import chain
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


# This fixture enables loading custom integrations in all tests.
# Remove to enable selective use of this fixture
@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: bool) -> None:  # noqa: ARG001, FBT001
    """Enable loading custom components."""
    return


@pytest.fixture(autouse=True)
def sleep() -> Generator[AsyncMock]:
    """Disable sleep for all tests."""
    with patch("custom_components.retry.asyncio.sleep") as mock:
        yield mock


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Any:
    """Ensure there are no warnings or higher severity log entries."""
    marker = item.get_closest_marker("allowed_logs")
    allowed_logs = marker.args[0] if marker and marker.args else ()
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                records.append(record)

    logger = logging.getLogger()
    handler = _Collector()
    logger.addHandler(handler)
    try:
        result = yield
    finally:
        logger.removeHandler(handler)

    for record in records:
        message = record.getMessage()
        if any(
            message.startswith(allowed_log)
            for allowed_log in chain(
                allowed_logs,
                ["We found a custom integration retry", "[Failed]: attempt"],
            )
        ):
            continue
        pytest.fail(f"Disallowed {record.levelname} log: {message}")

    return result
