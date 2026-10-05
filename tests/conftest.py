"""Fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    return


@pytest.fixture(autouse=True)
def _allow_sockets(socket_enabled):
    """The fake unit is a real localhost TCP server."""
    return
