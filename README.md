# Solar Cube Home Assistant Integration

Custom HACS-friendly integration that connects Home Assistant to your Solar Cube HEMS by reading metrics and forecasts from InfluxDB 2.x.

## Features
- Config-flow based setup for InfluxDB URL, token, organization, and bucket names.
- Sensors for live power, voltages, accumulated energy, SoC, prices, controller metadata, and optimisation savings.
- Attribute-rich sensors that expose hourly energy forecasts and optimal charge/discharge actions pulled directly from InfluxDB.
- Bundled Lovelace dashboards (`dashboards/panel_solar_cube_pl.yaml` / `dashboards/panel_solar_cube_en.yaml`, `dashboards/history_solar_cube_pl.yaml` / `dashboards/history_solar_cube_en.yaml`, `dashboards/forecasts_solar_cube_pl.yaml` / `dashboards/forecasts_solar_cube_en.yaml`) that can be auto-imported during setup.
- Optional automatic installation of the dashboard card dependencies listed in `dashboards/dependencies.json` when HACS is available.

## Compatibility
- Requires Home Assistant Core 2025.12.3 or newer.

## Installation (HACS)

If you previously added this repository as a custom repository in HACS, you can remove it from HACS → Integrations → Installed repositories.

Install via HACS (recommended — now included in the official HACS store):

1. Open HACS in Home Assistant and go to *Integrations → Explore & Add repositories*.
2. Search for **Solar Cube HEMS** and click *Install*.
3. Restart Home Assistant if prompted.
4. In **Settings → Devices & Services**, add the **Solar Cube** integration and provide:
   - InfluxDB URL (default: `http://influxdb2:8086`)
   - Token (optional if `influxdb_token` is set in `configuration.yaml`)
   - Organization (default: `solarcube`)
   - Buckets (defaults: `db` for live data, `agents` for forecasts/actions)

5. Add Local Calendar (optional, required for bundled automations):

	- Go to *Settings → Devices & Services → Add Integration* and select **Local Calendar**.
	- Create a calendar with the name "solar_cube" (recommended). The included automation uses `calendar.solar_cube` to create events; without this calendar the automation will not be able to create calendar events.

6. The integration will create the sensors automatically. By default it also registers the bundled dashboards in the sidebar (using the shipped YAML files under `dashboards/`). If you prefer to manage dashboards manually, disable **Import dashboards** in the setup form and then import the YAML files yourself.
7. Dashboard custom cards: the integration can optionally run a local installer hook to register/repair the required Lovelace resources. Leave **Run local frontend installer hook (advanced)** enabled during setup (default), or manage the resources yourself.

**WARNING — Automatic Frontend Resources**

- **What happens:** If you enable the local frontend installer hook the integration will automatically download a set of third-party Lovelace resources (JavaScript modules) and place them under `/hacsfiles/` on your Home Assistant configuration directory. The installer will also attempt to auto-add these resources to Home Assistant's Lovelace resource storage.
- **Why this matters:** These are external projects maintained by third parties. You should review the list below and confirm you are happy with the specified versions before enabling the installer.
- **Resources & versions downloaded:**
   - kalkih/mini-graph-card — mini-graph-card v0.13.0
   - flixlix/power-flow-card-plus — power-flow-card-plus v0.2.6
   - rejuvenate/lovelace-horizon-card — lovelace-horizon-card v1.4.0
   - totaldebug/atomic-calendar-revive — atomic-calendar-revive v10.0.0
   - mlamberts78/weather-chart-card — weather-chart-card V2.4.11
   - flixlix/energy-flow-card-plus — energy-flow-card-plus v0.1.2.1
   - SpangleLabs/history-explorer-card — history-explorer-card v1.0.54
   - hulkhaugen/hass-bha-icons — hass-bha-icons (installed from repository default branch)
   - MrBartusek/MeteoalarmCard — MeteoalarmCard v2.7.2
   - flixlix/energy-period-selector-plus — energy-period-selector-plus v0.2.3
   - zeronounours/lovelace-energy-entity-row — lovelace-energy-entity-row v1.2.0
   - RomRider/apexcharts-card — apexcharts-card v2.2.3
- **If you prefer to manage manually:** Do not enable the installer; instead add the listed resources manually in Home Assistant → Settings → Dashboards → Resources. After manual installation, hard-refresh the browser and restart Home Assistant if necessary.


## Branching
All development branches have been consolidated into `main`. If you previously tracked other branches, switch to `main` to ensure you have the latest dashboards, config flow options, and dependency handling.
