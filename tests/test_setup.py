"""End-to-end config entry setup, reload and unload."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_cube.const import (
    CONF_AGENTS_BUCKET,
    CONF_CONFIGURE_ENERGY_DASHBOARD,
    CONF_DATA_BUCKET,
    CONF_IMPORT_DASHBOARDS,
    CONF_ORG,
    CONF_RUN_FRONTEND_INSTALLER,
    CONF_S1_LCD_DISPLAY,
    DATA_ENTRIES,
    DOMAIN,
    OPT_ORPHAN_CLEANUP_DONE,
)

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME, CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant

ENTRY_DATA = {
    CONF_NAME: "Solar Cube",
    CONF_URL: "http://influx:8086",
    CONF_TOKEN: "tok",
    CONF_ORG: "solarcube",
    CONF_DATA_BUCKET: "db",
    CONF_AGENTS_BUCKET: "agents",
    CONF_IMPORT_DASHBOARDS: False,
    CONF_CONFIGURE_ENERGY_DASHBOARD: False,
    CONF_S1_LCD_DISPLAY: False,
    # Left at its default (True) this ran the real installer, which downloads
    # third-party JavaScript; the autouse guard in conftest now also blocks it.
    CONF_RUN_FRONTEND_INSTALLER: False,
}


@pytest.fixture
def mock_api():
    """Patch the InfluxDB client with canned readings."""
    with patch("custom_components.solar_cube.SolarCubeApi", autospec=True) as api_cls:
        api = api_cls.return_value
        api.async_query_last_batch = AsyncMock(
            return_value={
                "_sum/GridActivePower": 1234.0,
                "_sum/ProductionActivePower": 4200.0,
                "meter0/VoltageL1": 230_500,
                "_sum/ProductionActiveEnergy": 500_000.0,
                "cs/prices/buy_total_price_per_kwh": 0.87,
            }
        )
        api.async_get_forecast = AsyncMock(return_value=[])
        api.async_get_optimal_actions = AsyncMock(return_value=[])
        api.close = MagicMock()
        yield api


@pytest.fixture
async def entry(hass: HomeAssistant, mock_api) -> MockConfigEntry:
    config_entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, title="Solar Cube", data=ENTRY_DATA
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


class TestSetup:
    async def test_entry_loads(self, hass, entry) -> None:
        assert entry.state is ConfigEntryState.LOADED

    async def test_runtime_state_is_namespaced_under_entries(
        self, hass, entry
    ) -> None:
        assert entry.entry_id in hass.data[DOMAIN][DATA_ENTRIES]
        # Bookkeeping flags must never sit next to entry ids.
        assert entry.entry_id not in {
            k for k in hass.data[DOMAIN] if k != DATA_ENTRIES
        }

    async def test_sensors_are_created_and_grouped_into_one_device(
        self, hass, entry
    ) -> None:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        assert len(entities) > 50

        dev_reg = dr.async_get(hass)
        devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        assert len(devices) == 1
        assert devices[0].name == "Solar Cube"

        # Every entity belongs to that single device.
        assert {e.device_id for e in entities} == {devices[0].id}

    async def test_scaled_value_reaches_the_state_machine(self, hass, entry) -> None:
        state = hass.states.get("sensor.solar_cube_grid_l1_voltage")
        assert state is not None
        # 230500 raw / division 1000
        assert float(state.state) == 230.5

    async def test_unique_ids_are_stable_across_reinstalls(self, hass, entry) -> None:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        # Derived from entry.unique_id, not the volatile entry_id.
        assert all(e.unique_id.startswith("solar_cube_") for e in entities)
        assert not any(entry.entry_id in e.unique_id for e in entities)

    async def test_orphan_cleanup_marker_is_recorded(self, hass, entry) -> None:
        assert entry.options.get(OPT_ORPHAN_CLEANUP_DONE) is True


class TestUnload:
    async def test_unload_clears_state_and_closes_the_client(
        self, hass, entry, mock_api
    ) -> None:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.NOT_LOADED
        assert entry.entry_id not in hass.data[DOMAIN][DATA_ENTRIES]
        mock_api.close.assert_called_once()

    async def test_module_flags_are_reset_once_the_last_entry_goes(
        self, hass, entry
    ) -> None:
        """Regression: the "any entries left?" check compared against a dict that
        also held bookkeeping flags, so it was never true and this never ran."""
        from custom_components.solar_cube.const import DATA_AUTOMATIONS_IMPORTED

        hass.data[DOMAIN][DATA_AUTOMATIONS_IMPORTED] = True

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert DATA_AUTOMATIONS_IMPORTED not in hass.data[DOMAIN]

    async def test_reload_round_trips(self, hass, entry) -> None:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED


class TestSetupFailure:
    async def test_client_is_closed_when_the_first_refresh_fails(self, hass) -> None:
        """A retried setup must not leak an InfluxDB connection pool each time."""
        from custom_components.solar_cube.api import SolarCubeApiRequestError

        with patch(
            "custom_components.solar_cube.SolarCubeApi", autospec=True
        ) as api_cls:
            api = api_cls.return_value
            api.async_query_last_batch = AsyncMock(
                side_effect=SolarCubeApiRequestError("influx down")
            )
            api.close = MagicMock()

            config_entry = MockConfigEntry(
                domain=DOMAIN, unique_id=DOMAIN, title="Solar Cube", data=ENTRY_DATA
            )
            config_entry.add_to_hass(hass)
            await hass.config_entries.async_setup(config_entry.entry_id)
            await hass.async_block_till_done()

        assert config_entry.state is ConfigEntryState.SETUP_RETRY
        api.close.assert_called_once()

    async def test_auth_failure_starts_a_reauth_flow(self, hass) -> None:
        from custom_components.solar_cube.api import SolarCubeApiAuthError

        with patch(
            "custom_components.solar_cube.SolarCubeApi", autospec=True
        ) as api_cls:
            api = api_cls.return_value
            api.async_query_last_batch = AsyncMock(side_effect=SolarCubeApiAuthError())
            api.close = MagicMock()

            config_entry = MockConfigEntry(
                domain=DOMAIN, unique_id=DOMAIN, title="Solar Cube", data=ENTRY_DATA
            )
            config_entry.add_to_hass(hass)
            await hass.config_entries.async_setup(config_entry.entry_id)
            await hass.async_block_till_done()

        assert config_entry.state is ConfigEntryState.SETUP_ERROR
        flows = hass.config_entries.flow.async_progress()
        assert any(f["context"]["source"] == "reauth" for f in flows)


class TestLcdGating:
    async def test_lcd_is_skipped_when_pillow_is_missing(
        self, hass, mock_api, caplog
    ) -> None:
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=DOMAIN,
            title="Solar Cube",
            data={**ENTRY_DATA, CONF_S1_LCD_DISPLAY: True},
        )
        config_entry.add_to_hass(hass)

        with patch(
            "custom_components.solar_cube._pillow_available", return_value=False
        ):
            assert await hass.config_entries.async_setup(config_entry.entry_id)
            await hass.async_block_till_done()

        assert "Pillow is not installed" in caplog.text
        assert (
            "lcd_controller" not in hass.data[DOMAIN][DATA_ENTRIES][
                config_entry.entry_id
            ]
        )


class TestLcdCurrency:
    """The panel must agree with the monetary sensors."""

    async def _setup_with_currency(self, hass, mock_api, currency):
        hass.config.currency = currency
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=DOMAIN,
            title="Solar Cube",
            data={**ENTRY_DATA, CONF_S1_LCD_DISPLAY: True},
        )
        entry.add_to_hass(hass)
        with patch(
            "custom_components.solar_cube.SolarCubeLCDController", autospec=True
        ) as controller:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        return controller

    async def test_configured_currency_reaches_the_renderer(
        self, hass, mock_api
    ) -> None:
        controller = await self._setup_with_currency(hass, mock_api, "EUR")
        assert controller.call_args.args[5] == "EUR"

    async def test_unset_currency_falls_back_to_pln(self, hass, mock_api) -> None:
        from custom_components.solar_cube.const import DEFAULT_CURRENCY

        controller = await self._setup_with_currency(hass, mock_api, "")
        assert controller.call_args.args[5] == DEFAULT_CURRENCY

    async def test_the_panel_and_the_sensors_agree(self, hass, mock_api) -> None:
        """Regression guard: the panel used a hard-coded currency while the
        sensors used the configured one, so they could disagree."""
        hass.config.currency = "EUR"
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=DOMAIN, title="Solar Cube", data=ENTRY_DATA
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        from custom_components.solar_cube.solar_lcd import currency_label

        sensor = hass.states.get("sensor.solar_cube_optimised_energy_total_savings")
        assert sensor.attributes["unit_of_measurement"] == "EUR"
        assert currency_label(hass.config.currency, "en") == "EUR"


class TestFrontendInstallerNeverRunsUnderTest:
    """The installer shells out to a script that downloads third-party
    JavaScript. The option defaults to True, because Solar Cube ships as an
    appliance whose users cannot install Lovelace cards themselves, so any
    fixture setting up an entry without disabling it ran the real thing --
    pytest_socket cannot intervene, since the traffic belongs to a subprocess
    rather than to this interpreter, and the spawned process left an asyncio
    waitpid thread that surfaced as an intermittent teardown error.
    """

    async def test_setting_up_an_entry_spawns_no_process(self, hass) -> None:
        import asyncio

        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=DOMAIN,
            title="Solar Cube",
            data={**ENTRY_DATA, CONF_RUN_FRONTEND_INSTALLER: True},
        )
        entry.add_to_hass(hass)

        with (
            patch(
                "custom_components.solar_cube.SolarCubeApi", autospec=True
            ) as api_cls,
            patch.object(asyncio, "create_subprocess_exec") as spawn,
        ):
            api = api_cls.return_value
            api.async_validate = AsyncMock(return_value=None)
            api.async_query_last_batch = AsyncMock(return_value={})
            api.async_get_forecast = AsyncMock(return_value={})
            api.async_get_optimal_actions = AsyncMock(return_value={})
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        assert spawn.call_count == 0, (
            "entry setup spawned a subprocess; the frontend installer guard in "
            "conftest is not covering this path"
        )

    @pytest.mark.allow_frontend_installer
    async def test_the_guard_is_what_stops_it(self, hass) -> None:
        """Opting out reaches the real function, which does spawn. Shows the
        guard is load-bearing rather than decorative; the spawn is intercepted
        so nothing is fetched."""
        import asyncio

        from custom_components.solar_cube import _async_run_frontend_installer

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        with patch.object(
            asyncio, "create_subprocess_exec", return_value=FakeProc()
        ) as spawn:
            await _async_run_frontend_installer(hass)

        assert spawn.call_count == 1
        assert "install_frontend_deps.sh" in " ".join(
            str(a) for a in spawn.call_args[0]
        )
