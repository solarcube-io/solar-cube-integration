"""Solar Cube integration entry setup."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import yaml

from .api import SolarCubeApi
from .const import (
    CONF_AGENTS_BUCKET,
    CONF_CONFIGURE_ENERGY_DASHBOARD,
    CONF_DATA_BUCKET,
    CONF_IMPORT_DASHBOARDS,
    CONF_LANGUAGE,
    CONF_ORG,
    CONF_REAPPLY_DASHBOARDS,
    CONF_RUN_FRONTEND_INSTALLER,
    CONF_S1_LCD_BRIDGE_TOKEN,
    CONF_S1_LCD_BRIDGE_URL,
    CONF_S1_LCD_DISPLAY,
    DASHBOARD_DEPENDENCIES_PATH,
    DASHBOARD_SPECS,
    DATA_AUTOMATIONS_IMPORTED,
    DATA_ENTRIES,
    DATA_LOVELACE_RETRY_SCHEDULED,
    DATA_RESTART_NOTIFICATION_SHOWN,
    DEFAULT_CONFIGURE_ENERGY_DASHBOARD,
    DEFAULT_CURRENCY,
    DEFAULT_IMPORT_DASHBOARDS,
    DEFAULT_LANGUAGE,
    DEFAULT_REAPPLY_DASHBOARDS,
    DEFAULT_RUN_FRONTEND_INSTALLER,
    DEFAULT_S1_LCD_BRIDGE_TOKEN,
    DEFAULT_S1_LCD_BRIDGE_URL,
    DEFAULT_S1_LCD_DISPLAY,
    DOMAIN,
    ISSUE_DASHBOARDS_OUTDATED,
    ISSUE_RESTART_REQUIRED,
    OPT_ORPHAN_CLEANUP_DONE,
    OPT_SEEDED_DASHBOARDS,
    OPT_STORAGE_DASHBOARDS_IMPORTED,
    PACKAGED_DASHBOARDS_DIR,
    normalize_language,
)
from .coordinator import (
    COUNTER_GUARD_STORAGE_VERSION,
    SolarCubeDataCoordinator,
    SolarCubeForecastCoordinator,
    SolarCubeOptimalActionsCoordinator,
    counter_guard_storage_key,
)
from .sensor_definitions import SENSOR_DEFINITIONS
from .solar_lcd import SolarCubeLCDController

from homeassistant.components import persistent_notification
from homeassistant.components.frontend import async_register_built_in_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    CONF_TOKEN,
    CONF_URL,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.yaml import Secrets, load_yaml_dict

PLATFORMS = ["sensor"]

LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Keep at most this many timestamped backups of files we rewrite in .storage.
MAX_BACKUPS = 3

# Per-entry counter of update-listener callbacks to ignore, one per one-shot
# option write performed during setup.
SUPPRESSED_RELOADS = "suppressed_reloads"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (config entries only)."""
    return True


def _pillow_available() -> bool:
    """Return True when Pillow can be imported (blocking; run in an executor)."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _domain_data(hass: HomeAssistant) -> dict[str, Any]:
    """Return hass.data[DOMAIN], creating the expected shape on first use."""

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data.setdefault(DATA_ENTRIES, {})
    return domain_data


def _entry_state(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any] | None:
    """Return the per-entry runtime state dict, if the entry is loaded."""

    return _domain_data(hass)[DATA_ENTRIES].get(entry.entry_id)


def _apply_one_shot_options(
    hass: HomeAssistant, entry: ConfigEntry, changes: dict[str, Any]
) -> None:
    """Persist one-shot option changes without triggering a reload.

    A counter, not a flag: a single setup can perform several one-shot writes
    (orphan cleanup, dashboard import, energy dashboard), and Home Assistant
    delivers one update-listener callback per write. A boolean would swallow the
    first and let the rest reload the entry mid-setup.

    Only incremented when something actually changed, because
    ``async_update_entry`` does not notify listeners for a no-op write -- an
    unconditional increment would leave a stale count that eats the user's next
    genuine options change.
    """

    new_options = {**entry.options, **changes}
    if new_options == dict(entry.options):
        return

    if (state := _entry_state(hass, entry)) is not None:
        state[SUPPRESSED_RELOADS] = state.get(SUPPRESSED_RELOADS, 0) + 1
    hass.config_entries.async_update_entry(entry, options=new_options)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on option changes, except for our own one-shot writes."""

    state = _entry_state(hass, entry) or {}
    if state.get(SUPPRESSED_RELOADS, 0) > 0:
        state[SUPPRESSED_RELOADS] -= 1
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_run_frontend_installer(
    hass: HomeAssistant,
) -> tuple[int, str, str]:
    """Run the bundled installer hook script inside the HA environment."""
    script_path = Path(__file__).parent / "tools" / "install_frontend_deps.sh"

    if not await hass.async_add_executor_job(script_path.exists):
        return (127, "", f"Missing installer script: {script_path}")

    proc = await asyncio.create_subprocess_exec(
        "sh",
        str(script_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_b, stderr_b = await proc.communicate()
    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")
    return (proc.returncode or 0, stdout, stderr)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solar Cube from a config entry."""

    # Clear any previously-reported restart requirement. If we still need a restart
    # for this startup, we'll create the issue again.
    ir.async_delete_issue(hass, DOMAIN, ISSUE_RESTART_REQUIRED)

    # Options override entry.data so settings can be updated without removing the entry.
    config = {**entry.data, **entry.options}

    await _async_cleanup_orphaned_entities_once(hass, entry)

    api = SolarCubeApi(
        url=config[CONF_URL],
        token=config[CONF_TOKEN],
        org=config[CONF_ORG],
    )

    data_coordinator = SolarCubeDataCoordinator(
        hass, api, config, SENSOR_DEFINITIONS, entry_id=entry.entry_id
    )
    forecast_coordinator = SolarCubeForecastCoordinator(hass, api, config)
    optimal_coordinator = SolarCubeOptimalActionsCoordinator(hass, api, config)

    try:
        await data_coordinator.async_config_entry_first_refresh()
        await forecast_coordinator.async_config_entry_first_refresh()
        await optimal_coordinator.async_config_entry_first_refresh()
    except Exception:
        # Never leak the HTTP connection pool when setup fails or is retried.
        await hass.async_add_executor_job(api.close)
        raise

    domain_data = _domain_data(hass)
    domain_data[DATA_ENTRIES][entry.entry_id] = {
        "api": api,
        "data_coordinator": data_coordinator,
        "forecast_coordinator": forecast_coordinator,
        "optimal_coordinator": optimal_coordinator,
        CONF_DATA_BUCKET: config[CONF_DATA_BUCKET],
        CONF_AGENTS_BUCKET: config[CONF_AGENTS_BUCKET],
        CONF_NAME: config.get(CONF_NAME) or entry.title,
    }

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    installer_selected = bool(
        config.get(CONF_RUN_FRONTEND_INSTALLER, DEFAULT_RUN_FRONTEND_INSTALLER)
    )
    restart_needed = False

    # Optional: run the local installer hook once, then flip the flag off.
    if installer_selected:
        LOGGER.info(
            "Running frontend dependency installer hook (install_frontend_deps.sh) "
            "because '%s' was selected",
            CONF_RUN_FRONTEND_INSTALLER,
        )
        entry.async_create_background_task(
            hass,
            _async_run_installer_once(hass, entry),
            name=f"{DOMAIN}_frontend_installer",
        )

    # One-shot: overwrite everything this integration seeds into Home Assistant's
    # own storage, discarding local edits to it. Without this there is no way to
    # pick up a newer shipped dashboard once a copy exists.
    reapply = bool(config.get(CONF_REAPPLY_DASHBOARDS, DEFAULT_REAPPLY_DASHBOARDS))
    if reapply:
        LOGGER.info(
            "Re-applying shipped dashboards, automations and Energy dashboard "
            "because '%s' was selected",
            CONF_REAPPLY_DASHBOARDS,
        )

    if config.get(CONF_IMPORT_DASHBOARDS, DEFAULT_IMPORT_DASHBOARDS) or reapply:
        restart_needed |= await _async_ensure_storage_dashboards(
            hass, entry, config, reapply=reapply
        )
        restart_needed |= await _async_ensure_automations(hass, update=reapply)

    # Optional: one-shot configure the built-in Energy dashboard.
    if (
        config.get(CONF_CONFIGURE_ENERGY_DASHBOARD, DEFAULT_CONFIGURE_ENERGY_DASHBOARD)
        or reapply
    ):
        restart_needed |= await _async_configure_energy_dashboard(hass)
        _apply_one_shot_options(
            hass, entry, {CONF_CONFIGURE_ENERGY_DASHBOARD: False}
        )

    if reapply:
        _apply_one_shot_options(hass, entry, {CONF_REAPPLY_DASHBOARDS: False})

    # If we performed one-shot changes but did not run the installer, request restart now.
    if restart_needed and not installer_selected:
        _report_restart_required(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Optional: start Solar Cube PRO S1 LCD display controller.
    if config.get(CONF_S1_LCD_DISPLAY, DEFAULT_S1_LCD_DISPLAY):
        # Pillow ships with Home Assistant, so it is not declared as a
        # requirement for every user of this integration. Fail loudly rather
        # than silently rendering nothing if it is somehow unavailable.
        if not await hass.async_add_executor_job(_pillow_available):
            LOGGER.error(
                "The Solar Cube PRO S1 LCD display is enabled but Pillow is not "
                "installed; the display will stay off. Install Pillow or turn "
                "off '%s' in the integration options",
                CONF_S1_LCD_DISPLAY,
            )
            return True

        lang = normalize_language(config.get(CONF_LANGUAGE), DEFAULT_LANGUAGE)
        bridge_url = config.get(CONF_S1_LCD_BRIDGE_URL, DEFAULT_S1_LCD_BRIDGE_URL)
        bridge_token = config.get(
            CONF_S1_LCD_BRIDGE_TOKEN, DEFAULT_S1_LCD_BRIDGE_TOKEN
        )
        # Keep the panel in step with the monetary sensors, which use the same
        # resolution: a configured currency wins, otherwise PLN.
        configured = getattr(hass.config, "currency", None)
        currency = (
            configured.strip()
            if isinstance(configured, str) and configured.strip()
            else DEFAULT_CURRENCY
        )
        lcd_controller = SolarCubeLCDController(
            hass, entry, lang, bridge_url, bridge_token, currency
        )
        lcd_controller.start()
        domain_data[DATA_ENTRIES][entry.entry_id]["lcd_controller"] = lcd_controller
        LOGGER.info(
            "Solar Cube S1 LCD display activated (bridge=%s, lang=%s, currency=%s)",
            bridge_url,
            lang,
            currency,
        )

    return True


async def _async_run_installer_once(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Run the installer hook, disable the flag, then ask for a restart."""

    rc, stdout, stderr = await _async_run_frontend_installer(hass)
    if stdout:
        LOGGER.info("Frontend installer stdout:\n%s", stdout)
    if stderr:
        LOGGER.warning("Frontend installer stderr:\n%s", stderr)

    if rc != 0:
        # Leave the flag on so the next restart retries. Users of the appliance
        # have no shell and no way to re-trigger the install by hand, so giving
        # up permanently after one transient network failure would leave the
        # dashboards broken forever.
        LOGGER.warning(
            "Frontend dependency installer failed (exit %s); it will run again "
            "on the next Home Assistant restart",
            rc,
        )
        deps = await _load_dashboard_dependencies(hass)
        _notify_dependency_install(
            hass,
            deps,
            (
                "The local frontend installer hook failed and will retry on the "
                "next restart. Check Home Assistant logs for 'Frontend "
                "installer' output. You can also install the cards via HACS."
            ),
        )
        return

    # Succeeded: disable the one-shot flag so it does not re-run every restart.
    _apply_one_shot_options(hass, entry, {CONF_RUN_FRONTEND_INSTALLER: False})

    # Request a restart as the final step of the installation sequence.
    # Some Lovelace/dashboard pieces only fully settle after a restart.
    _report_restart_required(hass)


async def _async_cleanup_orphaned_entities_once(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """One-time removal of registry entries left by pre-0.2.0 uninstalls.

    Older versions derived unique_ids from ``entry.entry_id``, so reinstalling
    produced duplicate entities with ``_2``/``_3`` suffixes. This runs exactly
    once per config entry (tracked in the entry options) instead of on every
    startup, because unconditional pruning also discards user customisations
    such as renames, area assignments and hidden/disabled flags.
    """

    if entry.options.get(OPT_ORPHAN_CLEANUP_DONE):
        return

    ent_reg = er.async_get(hass)
    active_entry_ids = {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}

    removed = 0
    for entity_entry in list(ent_reg.entities.values()):
        if entity_entry.platform != DOMAIN:
            continue
        if (
            entity_entry.config_entry_id
            and entity_entry.config_entry_id not in active_entry_ids
        ):
            LOGGER.debug("Removing orphaned entity %s", entity_entry.entity_id)
            ent_reg.async_remove(entity_entry.entity_id)
            removed += 1

    if removed:
        LOGGER.info(
            "Removed %d orphaned Solar Cube entities left by a previous install",
            removed,
        )

    _apply_one_shot_options(hass, entry, {OPT_ORPHAN_CLEANUP_DONE: True})


def _load_yaml_list(path: Path) -> list[dict[str, Any]]:
    """Read a YAML file expected to contain a list of mappings (blocking)."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = yaml.safe_load(raw) if raw.strip() else []
    except yaml.YAMLError as err:
        LOGGER.warning("Invalid YAML in %s: %s", path, err)
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _prune_backups(pattern_dir: Path, prefix: str) -> None:
    """Keep only the newest MAX_BACKUPS files named ``<prefix>.bak.*`` (blocking)."""

    try:
        backups = sorted(pattern_dir.glob(f"{prefix}.bak.*"))
    except OSError:
        return
    for stale in backups[:-MAX_BACKUPS]:
        with contextlib.suppress(OSError):
            stale.unlink()


async def _async_ensure_automations(
    hass: HomeAssistant, update: bool = False
) -> bool:
    """Ensure Solar Cube automations exist in /config/automations.yaml.

    Best-effort merge of shipped automations from the packaged
    ``dashboards/automations.yaml``, deduplicated by non-empty 'id' (preferred)
    or 'alias'. Existing automations are left alone unless ``update`` is set, in
    which case a shipped automation replaces the local one with the same id --
    otherwise there is no way to pick up a corrected automation.
    """

    domain_data = _domain_data(hass)

    # Guard: only run once per HA runtime, unless explicitly re-applying.
    if domain_data.get(DATA_AUTOMATIONS_IMPORTED) and not update:
        return False
    domain_data[DATA_AUTOMATIONS_IMPORTED] = True

    shipped_path = PACKAGED_DASHBOARDS_DIR / "automations.yaml"
    config_path = Path(hass.config.config_dir) / "automations.yaml"

    def _read_merge_write() -> bool:
        if not shipped_path.exists():
            return False

        shipped = _load_yaml_list(shipped_path)
        if not shipped:
            return False

        existing = _load_yaml_list(config_path) if config_path.exists() else []

        existing_ids = {
            a["id"].strip()
            for a in existing
            if isinstance(a.get("id"), str) and a["id"].strip()
        }
        existing_aliases = {
            a["alias"].strip().lower()
            for a in existing
            if isinstance(a.get("alias"), str) and a["alias"].strip()
        }

        changed = False
        for automation in shipped:
            automation_id = str(automation.get("id") or "").strip()
            automation_alias = str(automation.get("alias") or "").strip()

            # Replace by id when re-applying, otherwise leave it alone.
            if automation_id and automation_id in existing_ids:
                if not update:
                    continue
                for index, current in enumerate(existing):
                    if str(current.get("id") or "").strip() == automation_id:
                        if current != automation:
                            existing[index] = automation
                            changed = True
                        break
                continue
            if (
                not automation_id
                and automation_alias
                and automation_alias.lower() in existing_aliases
            ):
                continue

            existing.append(automation)
            changed = True

        if not changed:
            return False

        try:
            config_path.write_text(
                yaml.safe_dump(existing, sort_keys=False, allow_unicode=True) + "\n",
                encoding="utf-8",
            )
        except OSError as err:
            LOGGER.warning("Failed writing %s: %s", config_path, err)
            return False

        return True

    changed = await hass.async_add_executor_job(_read_merge_write)
    if changed:
        LOGGER.info("Installed Solar Cube automations into %s", config_path)
        # Best-effort apply without a full restart.
        try:
            await hass.services.async_call("automation", "reload", {}, blocking=False)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("automation.reload failed: %s", err)

    return changed


async def _async_configure_energy_dashboard(hass: HomeAssistant) -> bool:
    """Best-effort configure the built-in Energy dashboard (.storage/energy).

    Uses the bundled ``dashboards/energy.json`` template and merges it into the
    existing Energy store, preserving unrelated fields.
    """

    storage_dir = Path(hass.config.config_dir) / ".storage"
    storage_path = storage_dir / "energy"
    template_path = PACKAGED_DASHBOARDS_DIR / "energy.json"

    def _load_template_data() -> dict[str, Any] | None:
        if not template_path.exists():
            LOGGER.warning("Energy dashboard template missing: %s", template_path)
            return None
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except OSError as err:
            LOGGER.warning("Failed reading energy template %s: %s", template_path, err)
            return None
        except json.JSONDecodeError as err:
            LOGGER.warning("Invalid JSON in energy template %s: %s", template_path, err)
            return None

        data = template.get("data")
        return data if isinstance(data, dict) else None

    template_data = await hass.async_add_executor_job(_load_template_data)
    if not template_data:
        return False

    def _read_modify_write() -> bool:
        try:
            storage_dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            LOGGER.warning("Cannot create %s: %s", storage_dir, err)
            return False

        raw = ""
        existing: dict[str, Any] = {}
        if storage_path.exists():
            try:
                raw = storage_path.read_text(encoding="utf-8")
                existing = json.loads(raw) if raw.strip() else {}
            except OSError as err:
                LOGGER.warning("Failed reading %s: %s", storage_path, err)
                return False
            except json.JSONDecodeError as err:
                LOGGER.warning("Invalid JSON in %s: %s", storage_path, err)
                return False

        if not isinstance(existing, dict):
            existing = {}

        current_data = existing.get("data")
        if not isinstance(current_data, dict):
            current_data = {}

        new_data = dict(current_data)
        # Replace energy_sources with the Solar Cube template.
        if isinstance(template_data.get("energy_sources"), list):
            new_data["energy_sources"] = template_data["energy_sources"]
        # Ensure required top-level keys exist.
        if "device_consumption" not in new_data:
            new_data["device_consumption"] = template_data.get("device_consumption", [])
        if "device_consumption_water" not in new_data:
            new_data["device_consumption_water"] = template_data.get(
                "device_consumption_water", []
            )

        if new_data == current_data and storage_path.exists():
            return False

        out = dict(existing)
        # Preserve existing version/minor_version if present; otherwise use template defaults.
        out.setdefault("version", 1)
        # Home Assistant's Energy store commonly uses minor_version 2.
        out.setdefault("minor_version", 2)
        out["key"] = "energy"
        out["data"] = new_data

        if raw:
            try:
                storage_path.with_name(f"energy.bak.{int(time.time())}").write_text(
                    raw, encoding="utf-8"
                )
                _prune_backups(storage_dir, "energy")
            except OSError:
                # Backups are best-effort.
                pass

        tmp_path = storage_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(storage_path)
        except OSError as err:
            LOGGER.warning("Failed writing %s: %s", storage_path, err)
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return False

        return True

    changed = await hass.async_add_executor_job(_read_modify_write)
    if changed:
        LOGGER.info("Configured Home Assistant Energy dashboard (%s)", storage_path)
    return changed


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the counter guard's saved state together with the entry."""

    await Store(
        hass, COUNTER_GUARD_STORAGE_VERSION, counter_guard_storage_key(entry.entry_id)
    ).async_remove()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and release its resources."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain_data = _domain_data(hass)
    entry_data = domain_data[DATA_ENTRIES].pop(entry.entry_id, None)
    if entry_data is None:
        return True

    # Stop LCD controller if running.
    if (lcd_controller := entry_data.get("lcd_controller")) is not None:
        await lcd_controller.async_stop()

    if (api := entry_data.get("api")) is not None:
        try:
            await hass.async_add_executor_job(api.close)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Error closing InfluxDB client: %s", err)

    # Only per-entry state lives under DATA_ENTRIES, so this check is now
    # accurate: module-level bookkeeping flags are siblings, not entries.
    if not domain_data[DATA_ENTRIES]:
        for key in (
            DATA_AUTOMATIONS_IMPORTED,
            DATA_LOVELACE_RETRY_SCHEDULED,
            DATA_RESTART_NOTIFICATION_SHOWN,
        ):
            domain_data.pop(key, None)

    return True


def _config_fingerprint(config: Any) -> str:
    """Stable hash of a dashboard config, for spotting local edits."""
    payload = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _async_ensure_storage_dashboards(
    hass: HomeAssistant, entry: ConfigEntry, config: dict[str, Any], reapply: bool = False
) -> bool:
    """Ensure Solar Cube dashboards exist as Lovelace Storage dashboards.

    Creates them as documents the user can edit afterwards, and keeps them
    current across upgrades. For each dashboard there are four cases, told apart
    by the fingerprint of whatever this integration last wrote:

    * absent            -> create and seed
    * unchanged shipped -> nothing to do
    * shipped changed, user's copy untouched -> refresh it silently
    * shipped changed, user's copy edited    -> leave it, raise a Repairs issue

    ``reapply`` forces the last case to overwrite, which is what the
    "Re-apply shipped dashboards" option does.

    Known limitation: Home Assistant does not expose the ``DashboardsCollection``
    instance owned by the running ``lovelace`` component, so a second instance
    has to be used. The live component keeps a stale in-memory list until the
    next restart, which is why a restart is requested afterwards.
    """

    domain_data = _domain_data(hass)
    seeded: dict[str, str] = dict(entry.options.get(OPT_SEEDED_DASHBOARDS) or {})
    outdated: list[str] = []

    # Lovelace may not be ready yet during startup.
    try:
        from homeassistant.components.lovelace.const import (  # type: ignore[import]
            CONF_ICON,
            CONF_REQUIRE_ADMIN,
            CONF_SHOW_IN_SIDEBAR,
            CONF_TITLE,
            CONF_URL_PATH,
            LOVELACE_DATA,
            MODE_STORAGE,
        )
        from homeassistant.components.lovelace.dashboard import (  # type: ignore[import]
            ConfigNotFound,
            DashboardsCollection,
            LovelaceStorage,
        )
    except ImportError as err:
        LOGGER.debug("Lovelace imports unavailable: %s", err)
        return False

    if LOVELACE_DATA not in hass.data:
        if not domain_data.get(DATA_LOVELACE_RETRY_SCHEDULED):
            domain_data[DATA_LOVELACE_RETRY_SCHEDULED] = True

            async def _retry(_: Any) -> None:
                domain_data.pop(DATA_LOVELACE_RETRY_SCHEDULED, None)
                if await _async_ensure_storage_dashboards(hass, entry, config):
                    _report_restart_required(hass)

            entry.async_on_unload(
                hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _retry)
            )
        return False

    changed = False
    # Any dashboard we could not import means the one-shot marker must NOT be
    # recorded, so the next restart tries again. Appliance users cannot
    # re-trigger the import themselves.
    failed = False

    # The packaged copy is authoritative: it is the version that shipped with
    # this release, so an upgrade must be able to reach the dashboards.
    #
    # It used to be the other way round, with /config/dashboards preferred. That
    # made shipped dashboards permanently unreachable on any install that had
    # been through v0.1.0, because v0.1.0 distributed those YAML files at the top
    # level of the repository and the install steps put them in /config/dashboards.
    # Later releases stopped shipping them there, so nothing ever replaced the
    # copies already on disk, and they kept winning -- including when re-applying,
    # which reads through this same function. Dashboards stayed at v0.1.0 no
    # matter what was installed over the top.
    #
    # Customisation belongs in the UI now: edit a dashboard and the fingerprint
    # check leaves your version alone (see below).
    stale_dir = Path(hass.config.config_dir) / "dashboards"
    lang = normalize_language(
        config.get(CONF_LANGUAGE), getattr(hass.config, "language", None)
    )
    dashboard_specs = DASHBOARD_SPECS[lang]

    def _resolve_source(filename: str) -> Path | None:
        """Return the packaged dashboard source, reporting ignored leftovers."""
        packaged = PACKAGED_DASHBOARDS_DIR / filename
        leftover = stale_dir / filename
        if packaged.is_file():
            if leftover.is_file():
                LOGGER.warning(
                    "Ignoring %s: it is a leftover from an older Solar Cube "
                    "release and is no longer used. The dashboard now comes from "
                    "%s. You can delete %s",
                    leftover,
                    packaged,
                    stale_dir,
                )
            return packaged
        # Only if this release did not ship the file at all.
        return leftover if leftover.is_file() else None

    dashboards_collection = DashboardsCollection(hass)
    await dashboards_collection.async_load()
    existing = {
        item.get(CONF_URL_PATH): item
        for item in dashboards_collection.async_items()
        if isinstance(item, dict)
    }

    lovelace_data = hass.data[LOVELACE_DATA]

    for spec in dashboard_specs:
        url_path = spec["url_path"]

        source_path = await hass.async_add_executor_job(
            _resolve_source, spec["filename"]
        )
        if source_path is None:
            LOGGER.warning(
                "Solar Cube storage dashboard import skipped for %s: %s not found "
                "in %s or %s",
                url_path,
                spec["filename"],
                PACKAGED_DASHBOARDS_DIR,
                stale_dir,
            )
            failed = True
            continue

        try:
            config_dict = await hass.async_add_executor_job(
                load_yaml_dict,
                str(source_path),
                Secrets(Path(hass.config.config_dir)),
            )
        except Exception as err:  # noqa: BLE001
            LOGGER.warning(
                "Solar Cube failed to read dashboard source %s: %s", source_path, err
            )
            failed = True
            continue

        shipped = _config_fingerprint(config_dict)

        item = existing.get(url_path)
        if item is None:
            try:
                item = await dashboards_collection.async_create_item(
                    {
                        CONF_TITLE: spec["title"],
                        CONF_URL_PATH: url_path,
                        CONF_ICON: spec["icon"],
                        CONF_SHOW_IN_SIDEBAR: True,
                        CONF_REQUIRE_ADMIN: False,
                    }
                )
            except Exception as err:  # noqa: BLE001
                LOGGER.warning(
                    "Solar Cube failed to create storage dashboard %s: %s",
                    url_path,
                    err,
                )
                failed = True
                continue
            changed = True

        store = LovelaceStorage(hass, item)
        try:
            stored = await store.async_load(False)
        except ConfigNotFound:
            stored = None
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Unexpected error loading dashboard %s: %s", url_path, err)
            failed = True
            continue

        write = False
        if stored is None:
            write = True
        elif shipped != seeded.get(url_path):
            if reapply or _config_fingerprint(stored) == seeded.get(url_path):
                # Either the user asked, or their copy is exactly what we last
                # wrote, so refreshing it discards nothing they authored.
                write = True
            else:
                outdated.append(spec["title"])
                LOGGER.info(
                    "Solar Cube dashboard '%s' has a newer shipped version, but the "
                    "local copy was edited; leaving it alone",
                    url_path,
                )

        if write:
            try:
                await store.async_save(config_dict)
            except Exception as err:  # noqa: BLE001
                LOGGER.warning(
                    "Failed saving storage dashboard config for %s: %s", url_path, err
                )
                failed = True
                continue
            seeded[url_path] = shipped
            changed = True
            LOGGER.info(
                "%s Lovelace Storage dashboard '%s' from %s",
                "Updated" if stored is not None else "Created",
                url_path,
                source_path,
            )
        elif url_path not in seeded:
            # First run against a dashboard that predates fingerprinting.
            seeded[url_path] = _config_fingerprint(stored) if stored else shipped

        # Register panel and expose it to Lovelace so it can be edited via UI.
        lovelace_data.dashboards[url_path] = store
        try:
            async_register_built_in_panel(
                hass,
                component_name="lovelace",
                frontend_url_path=url_path,
                config={"mode": MODE_STORAGE},
                sidebar_title=spec["title"],
                sidebar_icon=spec["icon"],
                require_admin=False,
            )
        except Exception as err:  # noqa: BLE001
            LOGGER.debug(
                "Failed registering storage dashboard panel %s: %s", url_path, err
            )

    if failed:
        LOGGER.warning(
            "Solar Cube dashboard import did not complete; it will be retried on "
            "the next Home Assistant restart"
        )
    else:
        _apply_one_shot_options(
            hass,
            entry,
            {
                OPT_STORAGE_DASHBOARDS_IMPORTED: True,
                OPT_SEEDED_DASHBOARDS: seeded,
            },
        )

    if outdated:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_DASHBOARDS_OUTDATED,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_DASHBOARDS_OUTDATED,
            translation_placeholders={"dashboards": ", ".join(outdated)},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_DASHBOARDS_OUTDATED)

    return changed


def _report_restart_required(hass: HomeAssistant) -> None:
    """Report a Repairs issue prompting the user to restart Home Assistant."""

    try:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_RESTART_REQUIRED,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_RESTART_REQUIRED,
            translation_placeholders={"integration": "Solar Cube HEMS"},
        )
        return
    except Exception as err:  # noqa: BLE001
        LOGGER.debug("Could not create repairs issue: %s", err)

    _notify_restart_required_fallback(hass)


def _notify_restart_required_fallback(hass: HomeAssistant) -> None:
    """Fallback restart prompt via persistent notification."""

    domain_data = _domain_data(hass)
    if domain_data.get(DATA_RESTART_NOTIFICATION_SHOWN):
        return
    domain_data[DATA_RESTART_NOTIFICATION_SHOWN] = True

    message = (
        "Solar Cube: Restart Home Assistant to finish installation.\n\n"
        "Some dashboards/resources may only load correctly after a restart.\n"
        "After the restart, hard-refresh your browser (Ctrl+F5 or Cmd+Shift+R) to "
        "load updated frontend resources.\n"
        "Go to Settings → System → Restart, or use Developer Tools → Services → "
        "homeassistant.restart.\n\n"
        "---\n\n"
        "Solar Cube: Zrestartuj Home Assistant, aby dokończyć instalację.\n\n"
        "Niektóre dashboardy/zasoby mogą działać poprawnie dopiero po restarcie.\n"
        "Po restarcie wykonaj twarde odświeżenie przeglądarki (Ctrl+F5 lub "
        "Cmd+Shift+R), aby wczytać zaktualizowane zasoby frontendu.\n"
        "Wejdź w Ustawienia → System → Restart lub użyj Narzędzia deweloperskie → "
        "Usługi → homeassistant.restart."
    )

    persistent_notification.async_create(
        hass,
        message,
        title="Solar Cube: restart required / wymagany restart",
        notification_id="solar_cube_restart_required",
    )


DEFAULT_DASHBOARD_DEPENDENCIES: list[dict[str, str]] = [
    {
        "name": "Energy Period Selector Plus",
        "repository": "flixlix/energy-period-selector-plus",
    },
    {"name": "Energy Flow Card Plus", "repository": "flixlix/energy-flow-card-plus"},
    {
        "name": "Energy Entity Row",
        "repository": "zeronounours/lovelace-energy-entity-row",
    },
    {"name": "Power Flow Card Plus", "repository": "flixlix/power-flow-card-plus"},
    {"name": "Horizon Card", "repository": "rejuvenate/lovelace-horizon-card"},
    {"name": "ApexCharts Card", "repository": "RomRider/apexcharts-card"},
    {"name": "Weather Chart Card", "repository": "mlamberts78/weather-chart-card"},
    {"name": "History Explorer Card", "repository": "SpangleLabs/history-explorer-card"},
    {"name": "Meteoalarm Card", "repository": "MrBartusek/MeteoalarmCard"},
    {"name": "Atomic Calendar Revive", "repository": "totaldebug/atomic-calendar-revive"},
]


async def _load_dashboard_dependencies(
    hass: HomeAssistant,
) -> list[dict[str, str]]:
    """Load the dashboard card dependency list, falling back to the defaults."""

    def _read() -> list[dict[str, str]] | None:
        if not DASHBOARD_DEPENDENCIES_PATH.is_file():
            return None
        try:
            data = json.loads(
                DASHBOARD_DEPENDENCIES_PATH.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as err:
            LOGGER.warning(
                "Failed to read dashboard dependencies from %s: %s",
                DASHBOARD_DEPENDENCIES_PATH,
                err,
            )
            return None

        if not isinstance(data, list):
            return None

        dependencies = [
            {
                "name": entry.get("name", entry["repository"]),
                "repository": entry["repository"],
            }
            for entry in data
            if isinstance(entry, dict) and "repository" in entry
        ]
        return dependencies or None

    return await hass.async_add_executor_job(_read) or DEFAULT_DASHBOARD_DEPENDENCIES


def _notify_dependency_install(
    hass: HomeAssistant, dependencies: list[dict[str, str]], reason: str
) -> None:
    """Tell the user which HACS cards the dashboards need."""

    dependency_list = "\n".join(
        f"- {item.get('name', item['repository'])} ({item['repository']})"
        for item in dependencies
    )

    persistent_notification.async_create(
        hass,
        (
            "Solar Cube dashboards require additional HACS cards. "
            f"{reason}\n\n"
            "How to install via HACS (UI):\n"
            "1) Open Home Assistant sidebar → HACS.\n"
            "   - If you don't see HACS: Settings → Add-ons / Integrations → HACS "
            "and ensure it's installed/configured.\n"
            "2) Go to Frontend (Lovelace) in HACS.\n"
            "3) For each repository below: Search / Explore & download repositories "
            "→ open it → Download.\n"
            "4) Reload the browser (or restart Home Assistant) after installing cards.\n\n"
            "Install the following repositories:\n"
            f"{dependency_list}"
            "\n\n---\n\n"
            "Dashboardy Solar Cube wymagają dodatkowych kart HACS.\n"
            "Jak zainstalować przez HACS (UI):\n"
            "1) Otwórz pasek boczny Home Assistant → HACS.\n"
            "   - Jeśli nie widzisz HACS: Ustawienia → Dodatki / Integracje → HACS "
            "i upewnij się, że jest zainstalowany/skonfigurowany.\n"
            "2) Wejdź w Frontend (Lovelace) w HACS.\n"
            "3) Dla każdego repozytorium poniżej: wyszukaj / Explore & download "
            "repositories → otwórz → Download.\n"
            "4) Po instalacji odśwież przeglądarkę (lub zrestartuj Home Assistant).\n\n"
            "Zainstaluj następujące repozytoria:\n"
            f"{dependency_list}"
        ),
        title="Solar Cube dashboard dependencies",
        notification_id="solar_cube_dashboard_dependencies",
    )
