"""Keeps the lifetime counters published to Home Assistant from going backwards.

The energy and savings totals come from OpenEMS's ``_sum`` (or the agent's
``cs``) counters. A lifetime counter only ever rises, but the value read from
InfluxDB can dip for a while and come back:

- while a component is missing, ``_sum`` adds up without it. On 2026-11-15 an
  OpenEMS restart left the Fronius inverters out of ``_sum/ProductionActiveEnergy``
  for about 70 s (1108.7 -> 384.9 MWh -> back); the day before, disabling two
  battery cabinets for tests did the same for 9 minutes;
- after a restart the persisted counter can come back a little lower and then
  count on from there.

Home Assistant takes a ``total_increasing`` value that falls by more than 10 % as
a meter reset and books the whole counter again when it comes back: one bar of
1.1 GWh in the Energy dashboard. The period meters, which rebase on any fall,
counted the comeback as production the same way. A smaller fall becomes a
negative and then a positive step.

So a fall is held: the last value is published until the source is back at it,
and only what the source rises beyond it counts. A source that stays below for
``REBASE_AFTER`` (a replaced or reset meter) is rebased instead: the published
value goes on from where it is with what the source adds from then on, never
dropping. If the missing part comes back later in one step, that step is not
counted either. The state lives in Home Assistant's storage, so a restart in the
middle of a dip does not publish it.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# How long a source may stay below the published value before it counts as a
# reset of the meter and is rebased.
REBASE_AFTER = timedelta(hours=24)
# After a rebase, a single rise of at least this share of the fall is the missing
# part coming back, not production.
RETURN_SHARE = 0.5


@dataclass
class _CounterState:
    # The last source value that counted, and published - source
    raw: float
    offset: float = 0.0
    # Since when the source has been below `raw` (ISO); None while it is not
    low_since: str | None = None
    # After a rebase: how far the source fell; None otherwise
    rebased_gap: float | None = None


class CounterGuard:
    """Holds the falls of the lifetime counters of one config entry."""

    def __init__(
        self,
        keys: Iterable[str],
        on_change: Callable[[], None] | None = None,
        rebase_after: timedelta = REBASE_AFTER,
    ) -> None:
        self._keys = frozenset(keys)
        self._on_change = on_change
        self._rebase_after = rebase_after
        self._states: dict[str, _CounterState] = {}

    @property
    def keys(self) -> frozenset[str]:
        """Return the guarded keys."""
        return self._keys

    def load(self, data: dict[str, Any] | None) -> None:
        """Restore the state saved by ``as_dict``."""

        for key, saved in (data or {}).items():
            if key not in self._keys or not isinstance(saved, dict):
                continue
            try:
                self._states[key] = _CounterState(
                    raw=float(saved["raw"]),
                    offset=float(saved.get("offset", 0.0)),
                    low_since=saved.get("low_since"),
                    rebased_gap=(
                        float(saved["rebased_gap"])
                        if saved.get("rebased_gap") is not None
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError):
                continue

    def as_dict(self) -> dict[str, Any]:
        """Return the state to save."""
        return {key: asdict(state) for key, state in self._states.items()}

    def apply(self, values: dict[str, Any], now: datetime | None = None) -> None:
        """Replace the guarded values in ``values`` by the values to publish.

        A missing or non-numeric value is left as it is and changes nothing.
        """

        now = now or dt_util.utcnow()
        changed = False
        for key in self._keys:
            try:
                value = float(values[key])
            except (KeyError, TypeError, ValueError):
                continue
            state = self._states.get(key)
            if state is None:
                self._states[key] = _CounterState(raw=value)
                changed = True
                continue
            changed |= self._update(key, state, value, now)
            values[key] = state.raw + state.offset if value < state.raw else value + state.offset
        if changed and self._on_change is not None:
            self._on_change()

    def _update(self, key: str, state: _CounterState, value: float, now: datetime) -> bool:
        """Update the state of one counter; return True if it changed beyond a rise."""

        if value >= state.raw:
            rise = value - state.raw
            changed = state.low_since is not None
            if changed:
                _LOGGER.info(
                    "%s is back at %.3f; its fall was not published", key, value
                )
            state.low_since = None
            if state.rebased_gap is not None and rise >= RETURN_SHARE * state.rebased_gap:
                # The part that fell away came back: not production
                _LOGGER.warning(
                    "%s rose by %.3f in one step after a rebase; not counted", key, rise
                )
                state.offset -= rise
                state.rebased_gap = None
                changed = True
            state.raw = value
            # A rise alone changes nothing worth saving, unless the offset is in use
            return changed or state.offset != 0.0
        if state.low_since is None:
            state.low_since = now.isoformat()
            _LOGGER.info(
                "%s fell from %.3f to %.3f; publishing %.3f until it is back",
                key,
                state.raw,
                value,
                state.raw + state.offset,
            )
            return True
        low_since = dt_util.parse_datetime(state.low_since)
        if low_since is not None and now - low_since >= self._rebase_after:
            _LOGGER.warning(
                "%s has stayed below %.3f since %s; rebasing on %.3f (meter reset)",
                key,
                state.raw,
                state.low_since,
                value,
            )
            state.offset += state.raw - value
            state.rebased_gap = state.raw - value
            state.raw = value
            state.low_since = None
            return True
        return False
