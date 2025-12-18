"""Solar Cube integration entry setup."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import yaml

from pathlib import Path

from homeassistant.components import persistent_notification
from homeassistant.components.frontend import async_register_built_in_panel, async_remove_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_TOKEN, CONF_URL
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.yaml import Secrets, load_yaml_dict

from .api import SolarCubeApi
from .const import (
    CONF_AGENTS_BUCKET,
    CONF_CONFIGURE_ENERGY_DASHBOARD,
    CONF_DATA_BUCKET,
    CONF_IMPORT_DASHBOARDS,
    CONF_ORG,
    CONF_RUN_FRONTEND_INSTALLER,
    DASHBOARD_FILES,
    DASHBOARD_DEPENDENCIES_PATH,
    DEFAULT_CONFIGURE_ENERGY_DASHBOARD,
    DEFAULT_IMPORT_DASHBOARDS,
    DOMAIN,
)
from .coordinator import (
    SolarCubeDataCoordinator,
    SolarCubeForecastCoordinator,
    SolarCubeOptimalActionsCoordinator,
)
from .sensor_definitions import SENSOR_DEFINITIONS

PLATFORMS = ["sensor"]

LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Allow internal one-shot option updates without triggering a reload loop.
    domain_data = hass.data.get(DOMAIN, {})
    entry_data = domain_data.get(entry.entry_id, {})
    if entry_data.pop("_suppress_next_reload", False):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_run_frontend_installer(hass: HomeAssistant) -> tuple[int, str, str]:
    """Run the bundled installer hook script inside the HA environment."""
    script_path = Path(__file__).parent / "tools" / "install_frontend_deps.sh"
    if not script_path.exists():
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
    # Clear any previously-reported restart requirement. If we still need a restart
    # for this startup, we'll create the issue again.
    _clear_restart_required_issue(hass)

    # Options override entry.data so settings can be updated without removing the entry.
    config = {**entry.data, **entry.options}

    api = SolarCubeApi(
        url=config[CONF_URL],
        token=config[CONF_TOKEN],
        org=config[CONF_ORG],
    )

    data_coordinator = SolarCubeDataCoordinator(hass, api, config, SENSOR_DEFINITIONS)
    forecast_coordinator = SolarCubeForecastCoordinator(hass, api, config)
    optimal_coordinator = SolarCubeOptimalActionsCoordinator(hass, api, config)

    await data_coordinator.async_config_entry_first_refresh()
    await forecast_coordinator.async_config_entry_first_refresh()
    await optimal_coordinator.async_config_entry_first_refresh()

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = {
        "api": api,
        "data_coordinator": data_coordinator,
        "forecast_coordinator": forecast_coordinator,
        "optimal_coordinator": optimal_coordinator,
        CONF_DATA_BUCKET: config[CONF_DATA_BUCKET],
        CONF_AGENTS_BUCKET: config[CONF_AGENTS_BUCKET],
        CONF_NAME: config.get(CONF_NAME) or entry.title,
    }

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    installer_selected = bool(config.get(CONF_RUN_FRONTEND_INSTALLER))
    restart_needed = False

    # Optional: run the local installer hook once, then flip the flag off.
    if installer_selected:
        LOGGER.warning(
            "Running frontend dependency installer hook (install_frontend_deps.sh) because '%s' was selected.",
            CONF_RUN_FRONTEND_INSTALLER,
        )

        async def _run_and_disable() -> None:
            rc, stdout, stderr = await _async_run_frontend_installer(hass)
            if stdout:
                LOGGER.info("Frontend installer stdout:\n%s", stdout)
            if stderr:
                LOGGER.warning("Frontend installer stderr:\n%s", stderr)

            if rc != 0:
                _notify_dependency_install(
                    hass,
                    _load_dashboard_dependencies(),
                    (
                        "The local frontend installer hook failed. "
                        "Check Home Assistant logs for 'Frontend installer' output."
                    ),
                )

            # Disable the one-shot flag to avoid re-running on every restart.
            entry_data = domain_data.get(entry.entry_id)
            if isinstance(entry_data, dict):
                entry_data["_suppress_next_reload"] = True
            hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, CONF_RUN_FRONTEND_INSTALLER: False},
            )

            # Request a restart as the final step of the installation sequence.
            # Some Lovelace/dashboard pieces only fully settle after a restart.
            _report_restart_required(hass)

        hass.async_create_task(_run_and_disable())

    if config.get(CONF_IMPORT_DASHBOARDS, DEFAULT_IMPORT_DASHBOARDS):
        restart_needed |= await _async_ensure_storage_dashboards(hass, domain_data)
        restart_needed |= await _async_ensure_automations(hass, domain_data)

    # Optional: one-shot configure the built-in Energy dashboard.
    if config.get(CONF_CONFIGURE_ENERGY_DASHBOARD, DEFAULT_CONFIGURE_ENERGY_DASHBOARD):
        restart_needed |= await _async_configure_energy_dashboard(hass)

        # Disable the one-shot flag to avoid re-writing on every restart.
        entry_data = domain_data.get(entry.entry_id)
        if isinstance(entry_data, dict):
            entry_data["_suppress_next_reload"] = True
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_CONFIGURE_ENERGY_DASHBOARD: False},
        )

    # If we performed one-shot changes but did not run the installer, request restart now.
    if restart_needed and not installer_selected:
        _report_restart_required(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_ensure_automations(hass: HomeAssistant, domain_data: dict[str, Any]) -> bool:
    """Ensure Solar Cube automations exist in /config/automations.yaml.

    Best-effort merge of shipped automations from custom_components/solar_cube/dashboards/automations.yaml.
    Does not overwrite existing automations; deduplicates by non-empty 'id' (preferred) or 'alias'.
    """

    # Guard: only run once per HA runtime.
    if domain_data.get("automations_imported"):
        return False
    domain_data["automations_imported"] = True

    shipped_path = Path(__file__).parent / "dashboards" / "automations.yaml"
    if not shipped_path.exists():
        return False

    config_path = Path(hass.config.config_dir) / "automations.yaml"

    def _load_yaml_list(path: Path) -> list[dict[str, Any]]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            data = yaml.safe_load(raw) if raw.strip() else []
        except Exception:  # noqa: BLE001
            return []
        if data is None:
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    shipped = await hass.async_add_executor_job(_load_yaml_list, shipped_path)
    if not shipped:
        return False

    def _read_merge_write() -> bool:
        existing = _load_yaml_list(config_path) if config_path.exists() else []

        existing_ids = {str(a.get("id")).strip() for a in existing if isinstance(a.get("id"), str) and a.get("id").strip()}
        existing_aliases = {str(a.get("alias")).strip().lower() for a in existing if isinstance(a.get("alias"), str) and a.get("alias").strip()}

        changed = False
        for automation in shipped:
            automation_id = str(automation.get("id") or "").strip()
            automation_alias = str(automation.get("alias") or "").strip()

            # Skip if already present.
            if automation_id and automation_id in existing_ids:
                continue
            if (not automation_id) and automation_alias and automation_alias.lower() in existing_aliases:
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
        LOGGER.warning("Installed Solar Cube automations into %s", Path(hass.config.config_dir) / "automations.yaml")
        # Best-effort apply without full restart.
        try:
            await hass.services.async_call("automation", "reload", {}, blocking=False)
        except Exception:  # noqa: BLE001
            pass

    return changed


async def _async_configure_energy_dashboard(hass: HomeAssistant) -> bool:
    """Best-effort configure the built-in Energy dashboard (.storage/energy).

    Uses a bundled template (custom_components/solar_cube/dashboards/energy.json)
    and merges it into the existing Energy store, preserving unrelated fields.
    """

    storage_dir = Path(hass.config.config_dir) / ".storage"
    storage_path = storage_dir / "energy"
    template_path = Path(__file__).parent / "dashboards" / "energy.json"

    if not template_path.exists():
        LOGGER.warning("Energy dashboard template missing: %s", template_path)
        return False

    def _load_template_data() -> dict[str, Any] | None:
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
            new_data["device_consumption_water"] = template_data.get("device_consumption_water", [])

        if new_data == current_data and storage_path.exists():
            return False

        out = dict(existing)
        # Preserve existing version/minor_version if present; otherwise use template defaults.
        if "version" not in out:
            out["version"] = 1
        if "minor_version" not in out:
            # Home Assistant's Energy store commonly uses minor_version 2.
            out["minor_version"] = 2
        out["key"] = "energy"
        out["data"] = new_data

        try:
            backup_path = storage_path.with_name(f"energy.bak.{int(time.time())}")
            if raw:
                backup_path.write_text(raw, encoding="utf-8")
        except Exception:  # noqa: BLE001
            # Backups are best-effort.
            pass

        tmp_path = storage_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(storage_path)
        except OSError as err:
            LOGGER.warning("Failed writing %s: %s", storage_path, err)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return False

        return True

    changed = await hass.async_add_executor_job(_read_modify_write)
    if changed:
        LOGGER.warning("Configured Home Assistant Energy dashboard (%s)", storage_path)
    return changed


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and entry.entry_id in hass.data.get(DOMAIN, {}):
        domain_data = hass.data[DOMAIN]
        entry_data = domain_data.pop(entry.entry_id)

        api = entry_data.get("api")
        if api is not None:
            try:
                api.close()
            except Exception:  # noqa: BLE001
                pass

        active_entries = {
            key for key in domain_data.keys() if key != "dashboards_registered"
        }

        if not active_entries:
            domain_data.pop("dependencies_installed", None)
            await _async_remove_dashboards(
                hass, domain_data.pop("dashboards_registered", set())
            )
    return unload_ok


async def _async_register_dashboards(hass: HomeAssistant, domain_data: dict[str, Any]) -> None:
    dashboard_dir = Path(__file__).parent.parent.parent / "dashboards"

    if not dashboard_dir.exists():
        return

    registered = domain_data.setdefault("dashboards_registered", set())

    for url_path, filename in DASHBOARD_FILES.items():
        dashboard_path = dashboard_dir / filename
        if not dashboard_path.exists() or url_path in registered:
            continue

        await async_register_built_in_panel(
            hass,
            component_name="lovelace",
            sidebar_title="Solar Cube",
            sidebar_icon="mdi:solar-panel",
            frontend_url_path=url_path,
            config={"mode": "yaml", "filename": str(dashboard_path)},
            require_admin=False,
        )
        registered.add(url_path)


async def _async_ensure_storage_dashboards(hass: HomeAssistant, domain_data: dict[str, Any]) -> bool:
    """Ensure Solar Cube dashboards exist as Lovelace Storage dashboards.

    This creates dashboards that are editable in the UI afterward.
    It is a one-shot best-effort import from YAML files under /config/dashboards.
    """

    # Lovelace may not be ready yet during startup.
    try:
        from homeassistant.components.lovelace.const import (  # type: ignore
            CONF_ICON,
            CONF_REQUIRE_ADMIN,
            CONF_SHOW_IN_SIDEBAR,
            CONF_TITLE,
            CONF_URL_PATH,
            LOVELACE_DATA,
            MODE_STORAGE,
        )
        from homeassistant.components.lovelace.dashboard import (  # type: ignore
            ConfigNotFound,
            DashboardsCollection,
            LovelaceStorage,
        )
    except Exception as err:  # noqa: BLE001
        LOGGER.debug("Lovelace imports unavailable: %s", err)
        return False

    if LOVELACE_DATA not in hass.data:
        if not domain_data.get("lovelace_retry_scheduled"):
            domain_data["lovelace_retry_scheduled"] = True

            async def _retry(_: Any) -> None:
                domain_data.pop("lovelace_retry_scheduled", None)
                await _async_ensure_storage_dashboards(hass, domain_data)

            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _retry)
        return False

    # Guard: only run once per HA runtime.
    if domain_data.get("storage_dashboards_imported"):
        return False
    domain_data["storage_dashboards_imported"] = True

    changed = False

    # Storage dashboards source files live in /config/dashboards.
    dashboards_dir = Path(hass.config.config_dir) / "dashboards"
    if not dashboards_dir.exists():
        try:
            dashboards_dir.mkdir(parents=True, exist_ok=True)
            LOGGER.debug("Created dashboards directory: %s", dashboards_dir)
        except OSError as err:
            LOGGER.warning(
                "Solar Cube storage dashboard import skipped: failed to create %s: %s",
                dashboards_dir,
                err,
            )
            return False

    # Fallback: dashboards bundled with the integration package.
    packaged_dashboards_dir = Path(__file__).parent / "dashboards"

    def _language_prefix() -> str:
        lang = (getattr(hass.config, "language", None) or "").lower()
        return (lang.split("-")[0] or "en").strip() or "en"

    is_pl = _language_prefix() == "pl"

    # Create 3 dashboards in storage mode with URL paths that satisfy HA validation.
    # (URL paths must be slug-like and contain a hyphen.)
    if is_pl:
        dashboard_specs: list[dict[str, str]] = [
            {
                "url_path": "panel-solar-cube",
                "title": "Solar Cube",
                "icon": "mdi:solar-panel",
                "filename": "panel_solar_cube_pl.yaml",
            },
            {
                "url_path": "historia-solar-cube",
                "title": "Solar Cube Historia",
                "icon": "mdi:history",
                "filename": "history_solar_cube_pl.yaml",
            },
            {
                "url_path": "prognozy-solar-cube",
                "title": "Solar Cube Prognozy",
                "icon": "mdi:weather-sunny-alert",
                "filename": "forecasts_solar_cube_pl.yaml",
            },
        ]
    else:
        dashboard_specs = [
            {
                "url_path": "panel-solar-cube",
                "title": "Solar Cube",
                "icon": "mdi:solar-panel",
                "filename": "panel_solar_cube_en.yaml",
            },
            {
                "url_path": "historia-solar-cube",
                "title": "Solar Cube History",
                "icon": "mdi:history",
                "filename": "history_solar_cube_en.yaml",
            },
            {
                "url_path": "prognozy-solar-cube",
                "title": "Solar Cube Forecasts",
                "icon": "mdi:weather-sunny-alert",
                "filename": "forecasts_solar_cube_en.yaml",
            },
        ]

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

        # If Lovelace already knows this dashboard, do nothing.
        if url_path in lovelace_data.dashboards:
            continue

        filename = spec["filename"]
        config_sources: list[Path] = []

        user_path = dashboards_dir / filename
        if user_path.exists():
            config_sources.append(user_path)

        packaged_path = packaged_dashboards_dir / filename
        if packaged_path.exists():
            config_sources.append(packaged_path)

        if not config_sources:
            LOGGER.warning(
                "Solar Cube storage dashboard import skipped for %s: missing %s and %s",
                url_path,
                user_path,
                packaged_path,
            )
            continue

        source_path = None
        config_dict = None
        last_err: Exception | None = None

        for candidate in config_sources:
            try:
                config_dict = await hass.async_add_executor_job(
                    load_yaml_dict,
                    str(candidate),
                    Secrets(Path(hass.config.config_dir)),
                )
                source_path = str(candidate)
                break
            except FileNotFoundError as err:
                last_err = err
                continue
            except Exception as err:  # noqa: BLE001
                last_err = err
                continue

        if config_dict is None or source_path is None:
            LOGGER.warning(
                "Solar Cube storage dashboard import failed to read any source for %s: %s",
                url_path,
                last_err,
            )
            continue

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
                continue
            changed = True

        store = LovelaceStorage(hass, item)
        # Only seed config if missing; never overwrite user edits.
        try:
            await store.async_load(False)
        except ConfigNotFound:
            try:
                await store.async_save(config_dict)
                LOGGER.warning(
                    "Created Lovelace Storage dashboard '%s' from %s",
                    url_path,
                    source_path,
                )
                changed = True
            except Exception as err:  # noqa: BLE001
                LOGGER.warning(
                    "Failed saving storage dashboard config for %s: %s",
                    url_path,
                    err,
                )
                continue
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Unexpected error loading dashboard %s: %s", url_path, err)
            continue

        # Register panel and expose it to Lovelace so it can be edited via UI.
        lovelace_data.dashboards[url_path] = store
        try:
            await async_register_built_in_panel(
                hass,
                component_name="lovelace",
                frontend_url_path=url_path,
                config={"mode": MODE_STORAGE},
                sidebar_title=spec["title"],
                sidebar_icon=spec["icon"],
                require_admin=False,
            )
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Failed registering storage dashboard panel %s: %s", url_path, err)

    return changed


def _report_restart_required(hass: HomeAssistant) -> None:
    """Report a Repairs issue prompting the user to restart Home Assistant."""

    try:
        from homeassistant.helpers import issue_registry as ir
        from homeassistant.helpers.issue_registry import IssueSeverity

        ir.async_create_issue(
            hass,
            DOMAIN,
            "restart_required",
            is_fixable=True,
            severity=IssueSeverity.WARNING,
            translation_key="restart_required",
            translation_placeholders={"integration": "Solar Cube HEMS"},
        )
        return
    except Exception:  # noqa: BLE001
        # Fall back to a persistent notification if Repairs/issue registry is unavailable.
        pass

    _notify_restart_required_fallback(hass)


def _clear_restart_required_issue(hass: HomeAssistant) -> None:
    """Remove the restart_required issue if present."""

    try:
        from homeassistant.helpers import issue_registry as ir

        ir.async_delete_issue(hass, DOMAIN, "restart_required")
    except Exception:  # noqa: BLE001
        pass


def _notify_restart_required_fallback(hass: HomeAssistant) -> None:
    """Fallback restart prompt via persistent notification."""

    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("restart_notification_shown"):
        return
    domain_data["restart_notification_shown"] = True

    message = (
        "Solar Cube: Restart Home Assistant to finish installation.\n\n"
        "Some dashboards/resources may only load correctly after a restart.\n"
        "After the restart, hard-refresh your browser (Ctrl+F5 or Cmd+Shift+R) to load updated frontend resources.\n"
        "Go to Settings → System → Restart, or use Developer Tools → Services → homeassistant.restart.\n\n"
        "---\n\n"
        "Solar Cube: Zrestartuj Home Assistant, aby dokończyć instalację.\n\n"
        "Niektóre dashboardy/zasoby mogą działać poprawnie dopiero po restarcie.\n"
        "Po restarcie wykonaj twarde odświeżenie przeglądarki (Ctrl+F5 lub Cmd+Shift+R), aby wczytać zaktualizowane zasoby frontendu.\n"
        "Wejdź w Ustawienia → System → Restart lub użyj Narzędzia deweloperskie → Usługi → homeassistant.restart."
    )

    persistent_notification.async_create(
        hass,
        message,
        title="Solar Cube: restart required / wymagany restart",
        notification_id="solar_cube_restart_required",
    )


def _load_dashboard_dependencies() -> list[dict[str, str]]:
    defaults = [
        {"name": "Energy Period Selector Plus", "repository": "flixlix/energy-period-selector-plus"},
        {"name": "Energy Flow Card Plus", "repository": "flixlix/energy-flow-card-plus"},
        {"name": "Energy Entity Row", "repository": "zeronounours/lovelace-energy-entity-row"},
        {"name": "Power Flow Card Plus", "repository": "flixlix/power-flow-card-plus"},
        {"name": "Horizon Card", "repository": "rejuvenate/lovelace-horizon-card"},
        {"name": "ApexCharts Card", "repository": "RomRider/apexcharts-card"},
        {"name": "Weather Chart Card", "repository": "mlamberts78/weather-chart-card"},
        {"name": "History Explorer Card", "repository": "alexarch21/history-explorer-card"},
        {"name": "Meteoalarm Card", "repository": "MrBartusek/MeteoalarmCard"},
        {"name": "Atomic Calendar Revive", "repository": "totaldebug/atomic-calendar-revive"},
    ]

    if not DASHBOARD_DEPENDENCIES_PATH.exists():
        return defaults

    try:
        data = json.loads(DASHBOARD_DEPENDENCIES_PATH.read_text())
    except (OSError, json.JSONDecodeError) as err:
        LOGGER.warning(
            "Failed to read dashboard dependencies from %s: %s",
            DASHBOARD_DEPENDENCIES_PATH,
            err,
        )
        return defaults

    dependencies: list[dict[str, str]] = []
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict) or "repository" not in entry:
                continue
            dependencies.append(
                {
                    "name": entry.get("name", entry["repository"]),
                    "repository": entry["repository"],
                }
            )

    return dependencies or defaults


def _notify_dependency_install(
    hass: HomeAssistant, dependencies: list[dict[str, str]], reason: str
) -> None:
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
            "   - If you don't see HACS: Settings → Add-ons / Integrations → HACS and ensure it's installed/configured.\n"
            "2) Go to Frontend (Lovelace) in HACS.\n"
            "3) For each repository below: Search / Explore & download repositories → open it → Download.\n"
            "4) Reload the browser (or restart Home Assistant) after installing cards.\n\n"
            "Install the following repositories:\n"
            f"{dependency_list}"
            "\n\n---\n\n"
            "Dashboardy Solar Cube wymagają dodatkowych kart HACS.\n"
            "Jak zainstalować przez HACS (UI):\n"
            "1) Otwórz pasek boczny Home Assistant → HACS.\n"
            "   - Jeśli nie widzisz HACS: Ustawienia → Dodatki / Integracje → HACS i upewnij się, że jest zainstalowany/skonfigurowany.\n"
            "2) Wejdź w Frontend (Lovelace) w HACS.\n"
            "3) Dla każdego repozytorium poniżej: wyszukaj / Explore & download repositories → otwórz → Download.\n"
            "4) Po instalacji odśwież przeglądarkę (lub zrestartuj Home Assistant).\n\n"
            "Zainstaluj następujące repozytoria:\n"
            f"{dependency_list}"
        ),
        title="Solar Cube dashboard dependencies",
        notification_id="solar_cube_dashboard_dependencies",
    )


async def _async_remove_dashboards(hass: HomeAssistant, dashboards: set[str]) -> None:
    for url_path in dashboards:
        await async_remove_panel(hass, url_path)
