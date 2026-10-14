"""Coordinators for Solar Cube."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from .api import SolarCubeApi, SolarCubeApiAuthError, SolarCubeApiRequestError
from .const import (
    CONF_AGENTS_BUCKET,
    CONF_DATA_BUCKET,
    DOMAIN,
    FORECAST_UPDATE_INTERVAL,
    OPTIMAL_ACTIONS_UPDATE_INTERVAL,
    UPDATE_INTERVAL,
)

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

LAST_UPDATE_KEY = "_last_update"

# Grouping key for batched queries: one InfluxDB round-trip per distinct group.
_QueryGroup = tuple[str, str, str]  # (bucket, measurement, range_start)


class _SolarCubeCoordinator(DataUpdateCoordinator[Any]):
    """Shared error translation for all Solar Cube coordinators."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarCubeApi,
        entry_data: dict[str, Any],
        name: str,
        update_interval: Any,
    ) -> None:
        self.api = api
        self.entry_data = entry_data
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{name}",
            update_interval=update_interval,
        )

    async def _async_update_data(self) -> Any:
        try:
            return await self._async_fetch()
        except SolarCubeApiAuthError as err:
            raise ConfigEntryAuthFailed("InfluxDB unauthorized") from err
        except SolarCubeApiRequestError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_fetch(self) -> Any:
        raise NotImplementedError

    @property
    def _agents_bucket(self) -> str:
        return self.entry_data[CONF_AGENTS_BUCKET]


class SolarCubeDataCoordinator(_SolarCubeCoordinator):
    """Coordinator for simple scalar values."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarCubeApi,
        entry_data: dict[str, Any],
        sensor_definitions: list[dict[str, Any]],
    ) -> None:
        self.sensor_definitions = sensor_definitions
        super().__init__(hass, api, entry_data, "data", UPDATE_INTERVAL)

    def _bucket_for(self, definition: dict[str, Any]) -> str:
        """Resolve a definition's logical source to the configured bucket name.

        Definitions declare ``source: "agents"`` or omit it for live data; the
        actual bucket names are user-configurable, so they must never be
        hard-coded in the definitions themselves.
        """

        if definition.get("source") == "agents":
            return self.entry_data[CONF_AGENTS_BUCKET]
        return self.entry_data[CONF_DATA_BUCKET]

    def _group_for(self, definition: dict[str, Any]) -> _QueryGroup:
        return (
            self._bucket_for(definition),
            definition["measurement"],
            definition.get("range_start", "-5m"),
        )

    def _query_groups(self) -> dict[_QueryGroup, dict[str, str]]:
        """Group sensor definitions into one batch per bucket/measurement/range.

        Returns a mapping of group -> {influx field name: sensor key}.
        """

        groups: dict[_QueryGroup, dict[str, str]] = defaultdict(dict)
        for definition in self.sensor_definitions:
            group = self._group_for(definition)
            groups[group][definition["field"]] = definition["key"]
        return groups

    def _fallback_groups(self) -> dict[_QueryGroup, dict[str, str]]:
        """Return group -> {sensor key: fallback influx field}.

        Kept apart from ``_query_groups`` so that a fallback field which is also
        another definition's primary field can never take over that
        definition's entry in the field -> key mapping.
        """

        groups: dict[_QueryGroup, dict[str, str]] = defaultdict(dict)
        for definition in self.sensor_definitions:
            if fallback := definition.get("fallback_field"):
                groups[self._group_for(definition)][definition["key"]] = fallback
        return groups

    async def _async_fetch(self) -> dict[str, Any]:
        groups = self._query_groups()
        fallback_groups = self._fallback_groups()

        async def _fetch_group(
            group: _QueryGroup, fields: dict[str, str]
        ) -> dict[str, Any]:
            bucket, measurement, range_start = group
            fallbacks = fallback_groups.get(group, {})
            raw = await self.api.async_query_last_batch(
                bucket=bucket,
                measurement=measurement,
                # Fallback fields ride along in the same query; dict.fromkeys
                # drops one that is also some definition's primary field.
                fields=list(dict.fromkeys([*fields, *fallbacks.values()])),
                range_start=range_start,
            )
            # Fields with no point in the window are reported as None.
            values = {key: raw.get(field) for field, key in fields.items()}
            # Decided on every poll: only a missing point falls back, a 0 stays.
            for key, fallback in fallbacks.items():
                if values.get(key) is None:
                    values[key] = raw.get(fallback)
            return values

        results = await asyncio.gather(
            *(_fetch_group(group, fields) for group, fields in groups.items())
        )

        values: dict[str, Any] = {}
        for chunk in results:
            values.update(chunk)
        values[LAST_UPDATE_KEY] = dt_util.utcnow().isoformat()
        return values


class SolarCubeForecastCoordinator(_SolarCubeCoordinator):
    """Coordinator for forecast data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarCubeApi,
        entry_data: dict[str, Any],
    ) -> None:
        super().__init__(hass, api, entry_data, "forecast", FORECAST_UPDATE_INTERVAL)

    async def _async_fetch(self) -> list[dict[str, Any]]:
        return await self.api.async_get_forecast(
            bucket=self._agents_bucket,
            hass_timezone=self.hass.config.time_zone,
        )


class SolarCubeOptimalActionsCoordinator(_SolarCubeCoordinator):
    """Coordinator for optimal actions data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarCubeApi,
        entry_data: dict[str, Any],
    ) -> None:
        super().__init__(
            hass, api, entry_data, "optimal_actions", OPTIMAL_ACTIONS_UPDATE_INTERVAL
        )

    async def _async_fetch(self) -> list[dict[str, Any]]:
        return await self.api.async_get_optimal_actions(
            bucket=self._agents_bucket,
            hass_timezone=self.hass.config.time_zone,
        )
