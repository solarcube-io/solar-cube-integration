# Solar Cube Home Assistant Integration

Custom HACS-friendly integration that connects Home Assistant to your Solar Cube HEMS by reading metrics and forecasts from InfluxDB 2.x.

## Features
- Config-flow based setup for InfluxDB URL, token, organization, and bucket names.
- Sensors for live power, voltages, accumulated energy, SoC, prices, controller metadata, and optimisation savings — all grouped under a single **Solar Cube** device.
- Attribute-rich sensors that expose hourly energy forecasts and optimal charge/discharge actions pulled directly from InfluxDB.
- Hourly/daily/weekly/monthly period meters that survive restarts and inverter counter resets.
- Bundled Lovelace dashboards (under `custom_components/solar_cube/dashboards/`) that can be auto-imported during setup as editable Storage dashboards.
- Optional Solar Cube PRO S1 LCD status display via the companion [Solar LCD Bridge](#solar-cube-pro-s1-lcd-display-optional) project.

## Compatibility
- Requires Home Assistant Core 2026.1.3 or newer, with frontend 20260107.2 or
  newer. The test suite runs against exactly that core version.

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

6. The integration will create the sensors automatically. By default it also imports the bundled dashboards as editable Lovelace Storage dashboards. If you prefer to manage dashboards manually, disable **Import dashboards** in the setup form.
7. Dashboard custom cards are downloaded automatically so the dashboards work out of the box — see the note below.

> **Note on dashboard import:** Home Assistant does not expose an API for adding Storage dashboards to an already-running `lovelace` component, so newly created dashboards appear in the sidebar only after the restart the integration prompts for. The import runs once; if any part of it fails it is retried on the next restart.

---

### ⚠️ Automatic frontend resources (on by default)

Solar Cube ships as an appliance: its users have no filesystem access and no way to install Lovelace cards by hand, so **"Automatically download the required dashboard cards" is enabled by default**. Untick it during setup if you would rather manage the cards yourself.

- **What happens:** the integration downloads a set of third-party Lovelace resources (JavaScript modules) into `/config/www/solar_cube` and registers them in Home Assistant's Lovelace resource storage under `/local/solar_cube/`. Those modules then run in every browser that opens your dashboards.
- **Why this matters:** these are external projects maintained by third parties, fetched over the network at setup time. Every download is version-pinned — review the list below. The installer also writes directly to `/config/.storage/lovelace_resources`, which is not a public Home Assistant API; a timestamped backup is kept next to it.
- **Retries:** if the download fails (no network, GitHub rate limit) the integration leaves the option enabled, raises a notification, and tries again on the next restart rather than leaving the dashboards permanently broken.
- **Resources & versions downloaded:**

  | Repository | Pinned version |
  | --- | --- |
  | kalkih/mini-graph-card | v0.13.0 |
  | flixlix/power-flow-card-plus | v0.2.6 |
  | rejuvenate/lovelace-horizon-card | v1.4.0 |
  | totaldebug/atomic-calendar-revive | v10.2.0 |
  | mlamberts78/weather-chart-card | V2.4.11 |
  | flixlix/energy-flow-card-plus | v0.1.2.1 |
  | SpangleLabs/history-explorer-card | v1.0.54 |
  | hulkhaugen/hass-bha-icons | commit `1868659` (upstream publishes no tags) |
  | MrBartusek/MeteoalarmCard | v2.7.2 |
  | flixlix/energy-period-selector-plus | v0.2.3 |
  | zeronounours/lovelace-energy-entity-row | v1.2.0 |
  | RomRider/apexcharts-card | v2.2.3 |

- **Cache-busting:** the installer appends `?v=<manifest version>` (fallback: a timestamp) to each resource URL, e.g. `/local/solar_cube/apexcharts-card/apexcharts-card.js?v=0.2.2`.
- **Managing them yourself instead:** untick the option and add the cards through HACS → Frontend, or manually under Settings → Dashboards → Resources. Hard-refresh the browser afterwards.

---

## Solar Cube PRO S1 LCD display (optional)

The integration can render a 170×320 status panel and push it to a Solar Cube PRO S1 LCD.

USB access lives in a **separate companion project**, the Solar LCD Bridge:

> <https://dev.azure.com/roygard/Solar%20Cube%20(Technology)/_git/Solar_LCD_Bridge>

The integration renders the frame and POSTs it to the bridge over HTTP; the bridge writes it to the panel over USB. The wire format, endpoints and shared default token are pinned in that repository's `CONTRACT.md`, and both repositories have tests guarding their own half of it.

Install the bridge from its own checkout:

```bash
sudo ./install.sh            # systemd service on the host
sudo ./install.sh --docker   # container image for DinD appliances
```

Then enable **Solar Cube PRO S1 LCD display** in the Solar Cube options. The defaults (`http://solar_lcd_bridge:8765` and the shared default token) match the bridge's shipped configuration, so no further setup is normally needed.

**Networking:** the bridge normally runs as a nested container under Docker-in-Docker, and Home Assistant reaches it from a sibling container over the shared Docker network, so it binds `0.0.0.0`. Loopback would only be reachable from inside the bridge's own container.

**Authentication:** every request carries a shared secret in the `X-Bridge-Token` header. The bridge image and this integration ship with the *same* published default token.

> That default is published in both repositories and is therefore **not a secret**. It keeps unrelated services and casual probes off the endpoint, nothing more. The real protections are that port 8765 is *not* published onto the host LAN and that the bridge is only reachable from the Docker network.

Where you can set environment variables, override it on both sides:

```bash
openssl rand -hex 32     # -> BRIDGE_TOKEN on the bridge,
                         #    and the "Solar LCD Bridge token" option here
```

**Currency:** the panel and the monetary sensors both follow the currency
configured in Home Assistant (Settings → System → General), falling back to
`PLN` when none is set. Polish shows the local symbol where one exists
(`zł`, `€`, `Kč`); English shows the ISO code (`PLN`, `EUR`), which is clearer
to a reader who is not local. A currency with no known symbol displays its code
in both languages.

Note that sensor units must be valid ISO 4217 codes — Home Assistant rejects
anything else for `device_class: monetary` — so the symbols are display-only and
never reach a sensor.

**Diagnostics:** when the display is enabled the integration probes the bridge
once at startup and checks that both sides speak the same protocol version and
agree on the frame size. Anything that will not fix itself -- a rejected token,
a version skew, a malformed bridge URL -- is reported in **Settings → Repairs**
and cleared automatically once a frame is accepted. A bridge that is simply not
running yet, or a panel that is unplugged, is treated as transient: the
integration keeps retrying, backing off to once a minute, and says so in the log
rather than raising a repair.

Pillow is required for the renderer. Home Assistant ships it, so it is not declared as an integration requirement; if it is missing the integration logs an error and leaves the display off.

### Previews

Rendered examples of every display state, in both languages, live in
[`docs/lcd_previews/`](docs/lcd_previews/) — start with the contact sheets.
Regenerate them with `python3 tools/preview_lcd.py` after changing the renderer,
the LCD strings or the fonts.

### Font Awesome icons

The LCD uses Font Awesome Solid glyphs. Pillow can only read `.woff2` when its bundled FreeType was compiled with brotli support, which is not guaranteed, so fetch the `.otf` desktop face:

```bash
./tools/fetch_fontawesome.sh
```

Without it the renderer falls back to simplified vector icons and logs a warning.

## Installing without HACS

For a device that cannot reach HACS, build a tarball and copy it across:

```bash
./scripts/build_release_archive.sh
```

See [docs/INSTALL_FROM_ARCHIVE.md](docs/INSTALL_FROM_ARCHIVE.md) for the transfer
and install steps, including the Docker and Docker-in-Docker cases.

## Development

```bash
pip install -r requirements-test.txt
pytest                      # unit tests
ruff check .                # lint
python scripts/validate_assets.py   # translations + dashboards parse and match
```

The 45 MB vendored Font Awesome web bundle under `tools/` is git-ignored; run `tools/fetch_fontawesome.sh` if you need it locally.

## Branching
All development branches have been consolidated into `main`. If you previously tracked other branches, switch to `main` to ensure you have the latest dashboards, config flow options, and dependency handling.
