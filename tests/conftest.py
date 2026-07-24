"""Shared fixtures. The pytest-homeassistant-custom-component plugin provides
the ``hass`` fixture; nothing project-specific is needed beyond enabling
custom-integration loading."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow the sensor_sentinel custom integration to be loaded."""
    yield
