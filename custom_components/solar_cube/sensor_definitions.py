"""Sensor definitions for Solar Cube.

Each definition describes one scalar value read from InfluxDB:

``key``          entity key, also the key in the coordinator's data dict
``name``         display name (suffixed to the device name by the entity)
``measurement``  InfluxDB measurement
``field``        InfluxDB field
``fallback_field``
                 optional InfluxDB field, fetched in the same query, whose value
                 is used when ``field`` has no point in the lookup window. Only
                 a missing point falls back; a real 0 from ``field`` is kept.
``source``       ``"agents"`` for the forecasts/actions bucket, omitted for the
                 live-data bucket. The concrete bucket names are user
                 configurable, so they are resolved by the coordinator and must
                 never be hard-coded here.
``range_start``  Flux range start for the lookup window (default ``-5m``)
``division``     raw value is divided by this before display
``unit``         native unit; the literal ``"currency"`` resolves to the Home
                 Assistant configured currency at entity-creation time
"""
from __future__ import annotations

from typing import Any

SENSOR_DEFINITIONS: list[dict[str, Any]] = [
    {
        "key": "grid_active_power",
        "name": "Grid Active Power",
        "measurement": "data",
        "field": "_sum/GridActivePower",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
    },
    {
        "key": "pv_active_power",
        "name": "PV Active Power",
        "measurement": "data",
        "field": "_sum/ProductionActivePower",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
    },
    {
        # The battery's own power, positive while discharging. EssActivePower is
        # the inverter's AC output, which on hybrid inverters also carries the DC
        # PV already counted in ProductionActivePower. Older GoodWe ET images
        # never write EssDischargePower, hence the fallback. The key keeps its
        # name so the entity_id the dashboards use does not change.
        "key": "ess_active_power",
        "name": "ESS Active Power",
        "measurement": "data",
        "field": "_sum/EssDischargePower",
        "fallback_field": "_sum/EssActivePower",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
    },
    {
        "key": "consumption_active_power",
        "name": "Consumption Active Power",
        "measurement": "data",
        "field": "_sum/ConsumptionActivePower",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
    },
    {
        "key": "grid_voltage_l1",
        "name": "Grid L1 Voltage",
        "measurement": "data",
        "field": "meter0/VoltageL1",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "division": 1000,
    },
    {
        "key": "grid_voltage_l2",
        "name": "Grid L2 Voltage",
        "measurement": "data",
        "field": "meter0/VoltageL2",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "division": 1000,
    },
    {
        "key": "grid_voltage_l3",
        "name": "Grid L3 Voltage",
        "measurement": "data",
        "field": "meter0/VoltageL3",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "division": 1000,
    },
    {
        "key": "grid_buy_active_energy",
        "name": "Grid Buy Active Energy",
        "measurement": "data",
        "field": "_sum/GridBuyActiveEnergy",
        "unit": "Wh",
        "device_class": "energy",
        "state_class": "total_increasing",
    },
    {
        "key": "grid_sell_active_energy",
        "name": "Grid Sell Active Energy",
        "measurement": "data",
        "field": "_sum/GridSellActiveEnergy",
        "unit": "Wh",
        "device_class": "energy",
        "state_class": "total_increasing",
    },
    {
        "key": "pv_active_energy",
        "name": "PV Active Energy",
        "measurement": "data",
        "field": "_sum/ProductionActiveEnergy",
        "unit": "Wh",
        "device_class": "energy",
        "state_class": "total_increasing",
    },
    {
        "key": "consumption_active_energy",
        "name": "Consumption Active Energy",
        "measurement": "data",
        "field": "_sum/ConsumptionActiveEnergy",
        "unit": "Wh",
        "device_class": "energy",
        "state_class": "total_increasing",
    },
    {
        "key": "ess_charge_energy",
        "name": "ESS Charge Energy",
        "measurement": "data",
        "field": "_sum/EssActiveChargeEnergy",
        "unit": "Wh",
        "device_class": "energy",
        "state_class": "total_increasing",
    },
    {
        "key": "ess_discharge_energy",
        "name": "ESS Discharge Energy",
        "measurement": "data",
        "field": "_sum/EssActiveDischargeEnergy",
        "unit": "Wh",
        "device_class": "energy",
        "state_class": "total_increasing",
    },
    {
        "key": "ess_soc",
        "name": "ESS SoC",
        "measurement": "data",
        "field": "_sum/EssSoc",
        "unit": "%",
        "device_class": "battery",
        "state_class": "measurement",
    },
    {
        "key": "buy_energy_price",
        "name": "Buy Energy Price",
        "measurement": "cs",
        "field": "cs/prices/buy_total_price_per_kwh",
        "unit": "per kWh",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "sell_energy_price",
        "name": "Sell Energy Price",
        "measurement": "cs",
        "field": "cs/prices/sell_price_per_kwh",
        "unit": "per kWh",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "controller_id",
        "name": "Controller ID",
        "measurement": "cs",
        "field": "cs/schedule/controller",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "target_soc",
        "name": "Target SoC",
        "measurement": "cs",
        "field": "cs/schedule/target_soc",
        "unit": "%",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "optimised_energy_total_savings",
        "name": "Optimised Energy Total Savings",
        "measurement": "cs",
        "field": "cs/prices/total_savings",
        "unit": "currency",
        "device_class": "monetary",
        "state_class": "total_increasing",
        "source": "agents",
        "range_start": "-1h",
    },
    # Scalar optimal action fields (also available inside the Optimal Actions payload).
    {
        "key": "optimal_actions_bc",
        "name": "Optimal Actions BC",
        "measurement": "cs",
        "field": "cs/opt_actions/bc",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "optimal_actions_bg",
        "name": "Optimal Actions BG",
        "measurement": "cs",
        "field": "cs/opt_actions/bg",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "optimal_actions_gb",
        "name": "Optimal Actions GB",
        "measurement": "cs",
        "field": "cs/opt_actions/gb",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "optimal_actions_gc",
        "name": "Optimal Actions GC",
        "measurement": "cs",
        "field": "cs/opt_actions/gc",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "optimal_actions_pb",
        "name": "Optimal Actions PB",
        "measurement": "cs",
        "field": "cs/opt_actions/pb",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "optimal_actions_pc",
        "name": "Optimal Actions PC",
        "measurement": "cs",
        "field": "cs/opt_actions/pc",
        "source": "agents",
        "range_start": "-60m",
    },
    {
        "key": "optimal_actions_pg",
        "name": "Optimal Actions PG",
        "measurement": "cs",
        "field": "cs/opt_actions/pg",
        "source": "agents",
        "range_start": "-60m",
    },
]

# Raw InfluxDB value -> display value scaling, derived from the definitions above.
# Shared by the sensor platform and the LCD renderer so the conversion lives in
# exactly one place.
DIVISIONS: dict[str, float] = {
    definition["key"]: float(definition["division"])
    for definition in SENSOR_DEFINITIONS
    if definition.get("division")
}


def scale_value(key: str, value: Any) -> Any:
    """Apply the configured division for ``key`` to a raw InfluxDB value.

    Returns the value unchanged when there is no division for the key or when it
    is not numeric.
    """

    if value is None:
        return None
    divisor = DIVISIONS.get(key)
    if not divisor:
        return value
    try:
        return float(value) / divisor
    except (TypeError, ValueError):
        return value
