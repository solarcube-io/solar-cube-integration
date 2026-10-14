"""Shared test fixtures for the Solar Cube integration."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_usb1_stub() -> None:
    """Provide a minimal ``usb1`` stub so the bridge is importable without libusb.

    The bridge calls ``sys.exit(1)`` when ``usb1`` is missing, which would abort
    collection on machines without libusb installed.
    """

    if "usb1" in sys.modules:
        return
    try:
        import usb1  # noqa: F401
    except ImportError:
        stub = types.ModuleType("usb1")
        stub.USBError = type("USBError", (Exception,), {})
        stub.USBContext = object
        stub.USBDeviceHandle = object
        sys.modules["usb1"] = stub


_install_usb1_stub()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Let Home Assistant load integrations from custom_components/."""
    return


@pytest.fixture(autouse=True)
def no_frontend_installer(request: pytest.FixtureRequest):
    """Stop the frontend installer from ever really running under test.

    It shells out to install_frontend_deps.sh, which downloads third-party
    JavaScript from GitHub into /config/www. The option defaults to True (Solar
    Cube ships as an appliance), so any fixture that sets up an entry without
    disabling it runs the script for real: pytest_socket cannot see it, because
    the traffic belongs to a subprocess rather than to this interpreter, and the
    spawned process leaves an asyncio waitpid thread that Home Assistant's
    cleanup check then reports at teardown.

    Tests that want to exercise the wiring ask for it with
    ``@pytest.mark.allow_frontend_installer`` and patch it themselves.
    """
    if request.node.get_closest_marker("allow_frontend_installer"):
        yield None
        return

    from unittest.mock import AsyncMock, patch

    with patch(
        "custom_components.solar_cube._async_run_frontend_installer",
        new_callable=AsyncMock,
        return_value=(0, "", ""),
    ) as mocked:
        yield mocked


@pytest.fixture
def utc_timezone():
    """Pin Home Assistant's local timezone to UTC.

    pytest-homeassistant-custom-component defaults to US/Pacific, which makes
    frozen UTC timestamps land on unexpected local days.
    """
    from homeassistant.util import dt as dt_util

    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(dt_util.UTC)
    try:
        yield dt_util.UTC
    finally:
        dt_util.set_default_time_zone(original)


class FakeRecord:
    """Stands in for an influxdb_client FluxRecord."""

    def __init__(self, field: str, value: Any, time: Any = None) -> None:
        self._field = field
        self._value = value
        self._time = time

    def get_field(self) -> str:
        return self._field

    def get_value(self) -> Any:
        return self._value

    def get_time(self) -> Any:
        return self._time


class FakeTable:
    """Stands in for an influxdb_client FluxTable."""

    def __init__(self, records: list[FakeRecord]) -> None:
        self.records = records


def make_tables(rows: list[tuple[str, Any, Any]]) -> list[FakeTable]:
    """Build one single-record table per row, as InfluxDB groups by _field."""
    return [FakeTable([FakeRecord(field, value, time)]) for field, value, time in rows]


class StubCoordinator:
    """Minimal stand-in for a DataUpdateCoordinator."""

    def __init__(self, data: Any = None) -> None:
        self.data = data
        self.last_update_success = True
        self.hass = None

    def async_add_listener(self, *_args: Any, **_kwargs: Any):
        return lambda: None
