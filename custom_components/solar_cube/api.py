"""API helpers for Solar Cube."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List

import influxdb_client
from influxdb_client.rest import ApiException

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


class SolarCubeApiAuthError(Exception):
    """Raised when InfluxDB returns an authentication error (401)."""


class SolarCubeApiRequestError(Exception):
    """Raised when an InfluxDB request fails for non-auth reasons."""


FORECAST_QUERY = """from(bucket: {bucket_literal})
  |> range(start: now(), stop: 32h)
  |> filter(fn: (r) => r["_measurement"] == "cs")
  |> filter(fn: (r) => r["_field"] == "cs/prices/buy_total_price_per_kwh" or r["_field"] == "cs/forecasts/consumption_forecast_kwh" or r["_field"] == "cs/forecasts/production_forecast_kwh" or r["_field"] == "cs/forecasts/soc_forecast" or r["_field"] == "cs/schedule/controller" or r["_field"] == "cs/schedule/target_soc" or r["_field"] == "cs/prices/sell_price_per_kwh")"""

OPTIMAL_ACTIONS_QUERY = """from(bucket: {bucket_literal})
  |> range(start: now(), stop: 32h)
  |> filter(fn: (r) => r["_measurement"] == "cs")
  |> filter(fn: (r) => r["_field"] == "cs/opt_actions/bc" or r["_field"] == "cs/opt_actions/bg" or r["_field"] == "cs/opt_actions/gb" or r["_field"] == "cs/opt_actions/gc" or r["_field"] == "cs/opt_actions/pb" or r["_field"] == "cs/opt_actions/pc" or r["_field"] == "cs/opt_actions/pg")"""


class SolarCubeApi:
    """Lightweight wrapper around influxdb-client."""

    def __init__(self, url: str, token: str, org: str) -> None:
        self._client = influxdb_client.InfluxDBClient(
            url=url,
            token=self._normalize_token(token),
            org=org,
            connection_pool_maxsize=64,
        )
        self._query_api = self._client.query_api()

    @staticmethod
    def _normalize_token(token: str) -> str:
        token = (token or "").strip()
        for prefix in ("Token ", "Bearer "):
            if token.startswith(prefix):
                return token[len(prefix) :].strip()
        return token

    @staticmethod
    def _flux_str_literal(value: str) -> str:
        """Return a Flux string literal (double-quoted) for a Python string."""
        return json.dumps(value)

    def _bucket_literal(self, bucket: str) -> str:
        return self._flux_str_literal((bucket or "").strip())

    async def async_validate(self, bucket: str | None = None) -> None:
        """Validate credentials by performing an authenticated call.

        Prefer validating via a lightweight query when a bucket is known,
        because some tokens might not have permission to list buckets.
        """
        try:
            if bucket:
                flux = (
                    f"from(bucket: {self._bucket_literal(bucket)}) "
                    "|> range(start: -1m) "
                    "|> limit(n: 1)"
                )
                _LOGGER.debug(
                    "Influx validate via query flux=%s (bucket_raw=%r)",
                    flux,
                    bucket,
                )
                await asyncio.to_thread(self._query_api.query, flux)
            else:
                buckets_api = self._client.buckets_api()
                await asyncio.to_thread(buckets_api.find_buckets)
        except ApiException as err:
            if getattr(err, "status", None) == 401:
                raise SolarCubeApiAuthError("Unauthorized") from err
            if getattr(err, "status", None) == 400:
                _LOGGER.error(
                    "InfluxDB rejected Flux (validate). details=%s",
                    self._api_exception_details(err),
                )
            raise SolarCubeApiRequestError(str(err)) from err

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _api_exception_details(err: ApiException) -> str:
        status = getattr(err, "status", None)
        reason = getattr(err, "reason", None)
        body = getattr(err, "body", None)
        # Keep log lines bounded.
        if isinstance(body, (bytes, bytearray)):
            try:
                body = body.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                body = str(body)
        if isinstance(body, str) and len(body) > 800:
            body = body[:800] + "…"
        return f"status={status} reason={reason} body={body!r}"

    async def async_query_last(
        self,
        bucket: str,
        measurement: str,
        field: str,
        range_start: str = "-5m",
    ) -> float | str | None:
        bucket_literal = self._bucket_literal(bucket)
        measurement_literal = self._flux_str_literal(measurement)
        field_literal = self._flux_str_literal(field)
        flux = (
            f"from(bucket: {bucket_literal}) "
            f"|> range(start: {range_start}) "
            f'|> filter(fn: (r) => r["_measurement"] == {measurement_literal}) '
            f'|> filter(fn: (r) => r["_field"] == {field_literal}) '
            "|> last()"
        )
        try:
            _LOGGER.debug(
                "Influx query_last flux=%s (bucket_raw=%r)",
                flux,
                bucket,
            )
            result = await asyncio.to_thread(self._query_api.query, flux)
        except ApiException as err:
            if getattr(err, "status", None) == 401:
                raise SolarCubeApiAuthError("Unauthorized") from err
            if getattr(err, "status", None) == 400:
                _LOGGER.error(
                    "InfluxDB rejected Flux (query_last). details=%s flux=%s",
                    self._api_exception_details(err),
                    flux,
                )
            raise SolarCubeApiRequestError(str(err)) from err
        for table in result:
            for record in table.records:
                return record.get_value()
        return None

    async def async_get_forecast(
        self, bucket: str, hass_timezone: str
    ) -> list[dict[str, Any]]:
        try:
            flux = FORECAST_QUERY.format(
                bucket_literal=self._bucket_literal(bucket)
            )
            _LOGGER.debug(
                "Influx forecast flux=%s (bucket_raw=%r)",
                flux,
                bucket,
            )
            result = await asyncio.to_thread(self._query_api.query, flux)
        except ApiException as err:
            if getattr(err, "status", None) == 401:
                raise SolarCubeApiAuthError("Unauthorized") from err
            if getattr(err, "status", None) == 400:
                _LOGGER.error(
                    "InfluxDB rejected Flux (forecast). details=%s flux=%s",
                    self._api_exception_details(err),
                    FORECAST_QUERY.format(
                        bucket_literal=self._bucket_literal(bucket)
                    ),
                )
            raise SolarCubeApiRequestError(str(err)) from err
        tz = dt_util.get_time_zone(hass_timezone)
        forecast_data: Dict[str, Dict[str, Any]] = {}

        for table in result:
            for record in table.records:
                record_time = record.get_time()
                if isinstance(record_time, str):
                    record_time = datetime.fromisoformat(record_time)
                local_time = record_time.astimezone(tz)
                hour_key = local_time.isoformat()
                if hour_key not in forecast_data:
                    forecast_data[hour_key] = {
                        "ctr": None,
                        "ts": None,
                        "cf": None,
                        "pf": None,
                        "sf": None,
                        "bp": None,
                        "sp": None,
                    }
                value = record.get_value()
                if isinstance(value, (float, int)):
                    value = round(value, 3)
                field = record.get_field()
                if field == "cs/schedule/controller":
                    forecast_data[hour_key]["ctr"] = value
                elif field == "cs/schedule/target_soc":
                    forecast_data[hour_key]["ts"] = value
                elif field == "cs/forecasts/consumption_forecast_kwh":
                    forecast_data[hour_key]["cf"] = value
                elif field == "cs/forecasts/production_forecast_kwh":
                    forecast_data[hour_key]["pf"] = value
                elif field == "cs/forecasts/soc_forecast":
                    forecast_data[hour_key]["sf"] = value
                elif field == "cs/prices/buy_total_price_per_kwh":
                    forecast_data[hour_key]["bp"] = value
                elif field == "cs/prices/sell_price_per_kwh":
                    forecast_data[hour_key]["sp"] = value

        return [
            {"dt": hour_key, **data}
            for hour_key, data in sorted(forecast_data.items())
        ]

    async def async_get_optimal_actions(
        self, bucket: str, hass_timezone: str
    ) -> List[dict[str, Any]]:
        try:
            flux = OPTIMAL_ACTIONS_QUERY.format(
                bucket_literal=self._bucket_literal(bucket)
            )
            _LOGGER.debug(
                "Influx optimal_actions flux=%s (bucket_raw=%r)",
                flux,
                bucket,
            )
            result = await asyncio.to_thread(self._query_api.query, flux)
        except ApiException as err:
            if getattr(err, "status", None) == 401:
                raise SolarCubeApiAuthError("Unauthorized") from err
            if getattr(err, "status", None) == 400:
                _LOGGER.error(
                    "InfluxDB rejected Flux (optimal_actions). details=%s flux=%s",
                    self._api_exception_details(err),
                    OPTIMAL_ACTIONS_QUERY.format(
                        bucket_literal=self._bucket_literal(bucket)
                    ),
                )
            raise SolarCubeApiRequestError(str(err)) from err
        tz = dt_util.get_time_zone(hass_timezone)
        actions: Dict[str, Dict[str, Any]] = {}

        for table in result:
            for record in table.records:
                record_time = record.get_time()
                if isinstance(record_time, str):
                    record_time = datetime.fromisoformat(record_time)
                local_time = record_time.astimezone(tz)
                hour_key = local_time.isoformat()
                if hour_key not in actions:
                    actions[hour_key] = {
                        "bc": None,
                        "bg": None,
                        "gb": None,
                        "gc": None,
                        "pb": None,
                        "pc": None,
                        "pg": None,
                    }
                value = record.get_value()
                if isinstance(value, (float, int)):
                    value = round(value, 3)
                field = record.get_field()
                short_key = field.split("/")[-1]
                actions[hour_key][short_key] = value

        return [
            {"dt": hour_key, **data}
            for hour_key, data in sorted(actions.items())
        ]
