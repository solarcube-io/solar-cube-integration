"""Tests for coordinator query batching and bucket resolution."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solar_cube.const import CONF_AGENTS_BUCKET, CONF_DATA_BUCKET
from custom_components.solar_cube.coordinator import SolarCubeDataCoordinator
from custom_components.solar_cube.sensor_definitions import SENSOR_DEFINITIONS

DEFINITIONS = [
    {"key": "a", "measurement": "data", "field": "f/a"},
    {"key": "b", "measurement": "data", "field": "f/b"},
    {
        "key": "c",
        "measurement": "cs",
        "field": "cs/c",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "d",
        "measurement": "cs",
        "field": "cs/d",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "e",
        "measurement": "cs",
        "field": "cs/e",
        "source": "agents",
        "range_start": "-1h",
    },
]

ENTRY_DATA = {CONF_DATA_BUCKET: "live-db", CONF_AGENTS_BUCKET: "my-agents"}


@pytest.fixture
def coordinator(hass) -> SolarCubeDataCoordinator:
    return SolarCubeDataCoordinator(hass, MagicMock(), ENTRY_DATA, DEFINITIONS)


class TestQueryGrouping:
    def test_definitions_collapse_into_one_group_per_bucket_measurement_range(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        groups = coordinator._query_groups()
        assert set(groups) == {
            ("live-db", "data", "-5m"),
            ("my-agents", "cs", "-60m"),
            ("my-agents", "cs", "-1h"),
        }
        assert groups[("live-db", "data", "-5m")] == {"f/a": "a", "f/b": "b"}
        assert groups[("my-agents", "cs", "-60m")] == {"cs/c": "c", "cs/d": "d"}

    def test_agents_source_resolves_to_the_configured_bucket(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        """Regression: the bucket used to be hard-coded to "agents" in the sensor
        definitions, so a user-configured agents bucket was silently ignored."""
        assert coordinator._bucket_for(DEFINITIONS[2]) == "my-agents"
        assert coordinator._bucket_for(DEFINITIONS[0]) == "live-db"


class TestFetch:
    async def test_one_query_per_group_not_per_sensor(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        coordinator.api.async_query_last_batch = AsyncMock(return_value={})
        await coordinator._async_fetch()
        assert coordinator.api.async_query_last_batch.await_count == 3

    async def test_values_are_keyed_by_sensor_key(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        async def fake_batch(*, bucket, measurement, fields, range_start):
            return {field: f"{field}-value" for field in fields}

        coordinator.api.async_query_last_batch = AsyncMock(side_effect=fake_batch)
        result = await coordinator._async_fetch()

        assert result["a"] == "f/a-value"
        assert result["c"] == "cs/c-value"
        assert "_last_update" in result

    async def test_missing_points_become_none(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        coordinator.api.async_query_last_batch = AsyncMock(return_value={"f/a": 1})
        result = await coordinator._async_fetch()
        assert result["a"] == 1
        assert result["b"] is None

    async def test_without_fallbacks_only_primary_fields_are_queried(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        coordinator.api.async_query_last_batch = AsyncMock(return_value={})
        await coordinator._async_fetch()

        assert coordinator._fallback_groups() == {}
        asked = {
            call.kwargs["range_start"]: call.kwargs["fields"]
            for call in coordinator.api.async_query_last_batch.await_args_list
        }
        assert asked == {
            "-5m": ["f/a", "f/b"],
            "-60m": ["cs/c", "cs/d"],
            "-1h": ["cs/e"],
        }


DISCHARGE = "_sum/EssDischargePower"
AC_OUTPUT = "_sum/EssActivePower"

FALLBACK_DEFINITIONS = [
    {
        "key": "ess",
        "measurement": "data",
        "field": DISCHARGE,
        "fallback_field": AC_OUTPUT,
    },
    {"key": "grid", "measurement": "data", "field": "_sum/GridActivePower"},
    {"key": "c", "measurement": "cs", "field": "cs/c", "source": "agents"},
]


def serve(coordinator: SolarCubeDataCoordinator, raw: dict) -> None:
    """Answer batch queries like InfluxDB: only fields that were asked for and
    have a point come back."""

    async def fake_batch(*, bucket, measurement, fields, range_start):
        return {field: raw[field] for field in fields if field in raw}

    coordinator.api.async_query_last_batch = AsyncMock(side_effect=fake_batch)


class TestFallbackField:
    @pytest.fixture
    def coordinator(self, hass) -> SolarCubeDataCoordinator:
        return SolarCubeDataCoordinator(
            hass, MagicMock(), ENTRY_DATA, FALLBACK_DEFINITIONS
        )

    async def test_primary_wins_even_when_the_fallback_differs(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        """Hybrid inverter: the AC output also carries DC PV, so it disagrees."""
        serve(coordinator, {DISCHARGE: -77800, AC_OUTPUT: -5800})
        assert (await coordinator._async_fetch())["ess"] == -77800

    async def test_missing_primary_uses_the_fallback(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        """Older GoodWe ET images never write EssDischargePower."""
        serve(coordinator, {AC_OUTPUT: -5800})
        assert (await coordinator._async_fetch())["ess"] == -5800

    async def test_zero_primary_is_a_reading_not_a_missing_point(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        serve(coordinator, {DISCHARGE: 0, AC_OUTPUT: -5800})
        assert (await coordinator._async_fetch())["ess"] == 0

    async def test_both_missing_is_none(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        serve(coordinator, {})
        assert (await coordinator._async_fetch())["ess"] is None

    async def test_fallback_is_decided_on_every_poll(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        serve(coordinator, {AC_OUTPUT: 1200})
        assert (await coordinator._async_fetch())["ess"] == 1200
        serve(coordinator, {DISCHARGE: 900, AC_OUTPUT: 1200})
        assert (await coordinator._async_fetch())["ess"] == 900
        serve(coordinator, {AC_OUTPUT: 300})
        assert (await coordinator._async_fetch())["ess"] == 300

    async def test_fallback_rides_in_the_same_batch_query(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        serve(coordinator, {})
        await coordinator._async_fetch()

        calls = coordinator.api.async_query_last_batch.await_args_list
        assert len(calls) == 2
        asked = {call.kwargs["measurement"]: call.kwargs["fields"] for call in calls}
        assert asked["data"] == [DISCHARGE, "_sum/GridActivePower", AC_OUTPUT]
        assert asked["cs"] == ["cs/c"]

    def test_fallback_stays_out_of_the_field_to_key_mapping(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        group = ("live-db", "data", "-5m")
        assert coordinator._query_groups()[group] == {
            DISCHARGE: "ess",
            "_sum/GridActivePower": "grid",
        }
        assert coordinator._fallback_groups() == {group: {"ess": AC_OUTPUT}}

    @pytest.mark.parametrize("plain_first", [True, False])
    async def test_fallback_shared_with_another_primary_keeps_both_sensors(
        self, hass, plain_first: bool
    ) -> None:
        """A fallback field that is also another sensor's own field must not
        take over that sensor's mapping, whichever definition comes first."""
        with_fallback = FALLBACK_DEFINITIONS[0]
        plain = {"key": "ac", "measurement": "data", "field": AC_OUTPUT}
        definitions = [plain, with_fallback] if plain_first else [with_fallback, plain]
        coordinator = SolarCubeDataCoordinator(
            hass, MagicMock(), ENTRY_DATA, definitions
        )

        serve(coordinator, {DISCHARGE: -77800, AC_OUTPUT: -5800})
        result = await coordinator._async_fetch()
        assert (result["ess"], result["ac"]) == (-77800, -5800)
        (call,) = coordinator.api.async_query_last_batch.await_args_list
        assert sorted(call.kwargs["fields"]) == sorted([DISCHARGE, AC_OUTPUT])

        serve(coordinator, {AC_OUTPUT: -5800})
        result = await coordinator._async_fetch()
        assert (result["ess"], result["ac"]) == (-5800, -5800)


class TestShippedBatteryPower:
    """ess_active_power must be the battery's own power. EssActivePower is the
    inverter's AC output, which on hybrid inverters with DC-coupled PV also
    carries PV already counted in ProductionActivePower."""

    @pytest.fixture
    def coordinator(self, hass) -> SolarCubeDataCoordinator:
        return SolarCubeDataCoordinator(
            hass, MagicMock(), ENTRY_DATA, SENSOR_DEFINITIONS
        )

    def test_entity_contract_is_unchanged(self) -> None:
        (definition,) = (
            d for d in SENSOR_DEFINITIONS if d["key"] == "ess_active_power"
        )
        assert definition["field"] == DISCHARGE
        assert definition["fallback_field"] == AC_OUTPUT
        assert definition["name"] == "ESS Active Power"
        assert definition["measurement"] == "data"
        assert "source" not in definition
        assert (definition["unit"], definition["device_class"]) == ("W", "power")
        assert definition["state_class"] == "measurement"

    async def test_hybrid_plant_reports_the_charging_battery(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        """Dyness hybrid cabinets plus Fronius: the card balances only with
        EssDischargePower (123.8 + 2.3 - 77.8 = 48.3 kW vs 49.3 kW load)."""
        serve(
            coordinator,
            {
                "_sum/ProductionActivePower": 123800,
                "_sum/GridActivePower": 2300,
                AC_OUTPUT: -5800,
                DISCHARGE: -77800,
                "_sum/ConsumptionActivePower": 49300,
            },
        )
        result = await coordinator._async_fetch()
        assert result["ess_active_power"] == -77800

    async def test_old_goodwe_image_falls_back_to_the_ac_output(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        serve(coordinator, {AC_OUTPUT: 2900})
        assert (await coordinator._async_fetch())["ess_active_power"] == 2900


class TestErrorTranslation:
    async def test_auth_error_triggers_reauth(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        from custom_components.solar_cube.api import SolarCubeApiAuthError

        from homeassistant.exceptions import ConfigEntryAuthFailed

        with (
            patch.object(
                coordinator, "_async_fetch", side_effect=SolarCubeApiAuthError()
            ),
            pytest.raises(ConfigEntryAuthFailed),
        ):
            await coordinator._async_update_data()

    async def test_request_error_becomes_update_failed(
        self, coordinator: SolarCubeDataCoordinator
    ) -> None:
        from custom_components.solar_cube.api import SolarCubeApiRequestError

        from homeassistant.helpers.update_coordinator import UpdateFailed

        with (
            patch.object(
                coordinator, "_async_fetch", side_effect=SolarCubeApiRequestError("x")
            ),
            pytest.raises(UpdateFailed),
        ):
            await coordinator._async_update_data()
