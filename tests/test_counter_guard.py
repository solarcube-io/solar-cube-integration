"""Tests for the guard that keeps the lifetime counters from going backwards."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import StubCoordinator
from freezegun import freeze_time

from custom_components.solar_cube.const import CONF_AGENTS_BUCKET, CONF_DATA_BUCKET
from custom_components.solar_cube.coordinator import (
    SolarCubeDataCoordinator,
    counter_guard_storage_key,
)
from custom_components.solar_cube.counter_guard import CounterGuard
from custom_components.solar_cube.sensor import SolarCubePeriodMeterSensor

from homeassistant.const import UnitOfEnergy

T0 = datetime(2026, 11, 15, 12, 45, tzinfo=UTC)


def publish(guard: CounterGuard, value, minutes: float = 0.0, key: str = "pv"):
    values = {key: value}
    guard.apply(values, T0 + timedelta(minutes=minutes))
    return values[key]


class TestCounterGuard:
    def test_rises_pass_through(self) -> None:
        guard = CounterGuard(["pv"])
        assert [publish(guard, v) for v in (1000.0, 1005.0, 1010.0)] == [1000.0, 1005.0, 1010.0]

    def test_a_dip_is_held_until_the_source_is_back(self) -> None:
        """2026-11-15: an OpenEMS restart left the Fronius out of _sum for about
        70 s; the PV counter fell from 1108.7 to 384.9 MWh and came back."""
        guard = CounterGuard(["pv"])
        published = [
            publish(guard, 1108661.92, 0.0),
            publish(guard, 384932.51, 1.0),
            publish(guard, 384935.0, 1.5),
            publish(guard, 1108668.0, 2.0),
            publish(guard, 1108670.0, 2.5),
        ]
        assert published == [1108661.92, 1108661.92, 1108661.92, 1108668.0, 1108670.0]

    def test_a_restart_that_comes_back_lower_is_not_counted_twice(self) -> None:
        guard = CounterGuard(["consumption"])
        values = [22736.018, 22735.263, 22735.9, 22736.5]
        published = [publish(guard, v, i, key="consumption") for i, v in enumerate(values)]
        assert published == [22736.018, 22736.018, 22736.018, 22736.5]

    def test_missing_values_pass_through_and_change_nothing(self) -> None:
        guard = CounterGuard(["pv"])
        assert publish(guard, 1000.0) == 1000.0
        assert publish(guard, None, 1) is None
        assert publish(guard, "unavailable", 2) == "unavailable"
        assert publish(guard, 1001.0, 3) == 1001.0

    def test_unguarded_keys_are_left_alone(self) -> None:
        guard = CounterGuard(["pv"])
        values = {"pv": 1000.0, "soc": 50.0}
        guard.apply(values, T0)
        values = {"pv": 900.0, "soc": 40.0}
        guard.apply(values, T0 + timedelta(minutes=1))
        assert values == {"pv": 1000.0, "soc": 40.0}

    def test_a_lasting_fall_is_rebased_without_a_spike(self) -> None:
        """A replaced or reset meter: after a day below, the counter goes on from
        where it was with what the source adds."""
        guard = CounterGuard(["pv"], rebase_after=timedelta(hours=24))
        assert publish(guard, 1000.0) == 1000.0
        assert publish(guard, 10.0, 1) == 1000.0
        assert publish(guard, 11.0, 60 * 23) == 1000.0
        assert publish(guard, 12.0, 60 * 25) == 1000.0
        assert publish(guard, 20.0, 60 * 26) == 1008.0

    def test_the_part_that_fell_away_coming_back_after_a_rebase_is_not_counted(self) -> None:
        guard = CounterGuard(["pv"], rebase_after=timedelta(hours=24))
        publish(guard, 1000.0)
        publish(guard, 10.0, 1)
        assert publish(guard, 12.0, 60 * 25) == 1000.0
        # The missing component is back: one step of 988
        assert publish(guard, 1000.0, 60 * 26) == 1000.0
        assert publish(guard, 1003.0, 60 * 27) == 1003.0

    def test_state_survives_a_restart_in_the_middle_of_a_dip(self) -> None:
        guard = CounterGuard(["pv"])
        publish(guard, 1000.0)
        publish(guard, 300.0, 1)
        restarted = CounterGuard(["pv"])
        restarted.load(guard.as_dict())
        assert publish(restarted, 305.0, 2) == 1000.0
        assert publish(restarted, 1002.0, 3) == 1002.0

    def test_load_ignores_unknown_keys_and_broken_entries(self) -> None:
        guard = CounterGuard(["pv"])
        guard.load({"other": {"raw": 5.0}, "pv": {"offset": 1.0}})
        assert guard.as_dict() == {}

    def test_saves_only_on_a_fall_a_recovery_or_a_rebase(self) -> None:
        on_change = MagicMock()
        guard = CounterGuard(["pv"], on_change=on_change)
        publish(guard, 1000.0)
        assert on_change.call_count == 1  # first state
        publish(guard, 1001.0, 1)
        publish(guard, 1002.0, 2)
        assert on_change.call_count == 1
        publish(guard, 500.0, 3)
        assert on_change.call_count == 2
        publish(guard, 1003.0, 4)
        assert on_change.call_count == 3


DEFINITIONS = [
    {
        "key": "pv_active_energy",
        "measurement": "data",
        "field": "_sum/ProductionActiveEnergy",
        "state_class": "total_increasing",
    },
    {"key": "ess_soc", "measurement": "data", "field": "_sum/EssSoc", "state_class": "measurement"},
]
ENTRY_DATA = {CONF_DATA_BUCKET: "db", CONF_AGENTS_BUCKET: "agents"}


def batches(*readings: dict[str, float]):
    async def fake_batch(*, bucket, measurement, fields, range_start):
        return readings_iter.pop(0)

    readings_iter = list(readings)
    return AsyncMock(side_effect=fake_batch)


class TestCoordinatorGuard:
    async def test_total_increasing_values_are_guarded(self, hass) -> None:
        coordinator = SolarCubeDataCoordinator(hass, MagicMock(), ENTRY_DATA, DEFINITIONS)
        assert coordinator.counter_guard.keys == {"pv_active_energy"}
        coordinator.api.async_query_last_batch = batches(
            {"_sum/ProductionActiveEnergy": 1108661920.0, "_sum/EssSoc": 93},
            {"_sum/ProductionActiveEnergy": 384932512.0, "_sum/EssSoc": 92},
        )
        await coordinator._async_fetch()
        result = await coordinator._async_fetch()
        assert result["pv_active_energy"] == 1108661920.0
        assert result["ess_soc"] == 92

    async def test_a_dip_in_progress_at_startup_is_held(self, hass, hass_storage) -> None:
        key = counter_guard_storage_key("entry1")
        hass_storage[key] = {
            "version": 1,
            "minor_version": 1,
            "key": key,
            "data": {
                "pv_active_energy": {
                    "raw": 1108661920.0,
                    "offset": 0.0,
                    "low_since": "2026-11-15T12:46:07+00:00",
                    "rebased_gap": None,
                }
            },
        }
        coordinator = SolarCubeDataCoordinator(
            hass, MagicMock(), ENTRY_DATA, DEFINITIONS, entry_id="entry1"
        )
        coordinator.api.async_query_last_batch = batches(
            {"_sum/ProductionActiveEnergy": 384932512.0, "_sum/EssSoc": 93}
        )
        result = await coordinator._async_fetch()
        assert result["pv_active_energy"] == 1108661920.0


class TestPeriodMeterBehindTheGuard:
    @freeze_time("2026-11-15 12:45:00")
    def test_the_daily_meter_does_not_count_a_dip(self) -> None:
        """2026-11-15: the daily PV meter rebased on the dip and counted the
        comeback as 723.7 MWh of production."""
        guard = CounterGuard(["pv_active_energy"])
        coordinator = StubCoordinator({})
        meter = SolarCubePeriodMeterSensor(
            coordinator,
            MagicMock(entry_id="abc", unique_id="solar_cube", title="Solar Cube"),
            key="daily_pv_energy",
            name="Daily PV Energy",
            source_key="pv_active_energy",
            source_unit="Wh",
            unit=UnitOfEnergy.KILO_WATT_HOUR,
            period="daily",
        )
        for minutes, raw in ((0, 1108000000.0), (1, 1108661920.0), (2, 384932512.0), (3, 1108668000.0)):
            values = {"pv_active_energy": raw}
            guard.apply(values, T0 + timedelta(minutes=minutes))
            coordinator.data = values
            meter._recalculate()
        assert meter.native_value == pytest.approx(668.0)
