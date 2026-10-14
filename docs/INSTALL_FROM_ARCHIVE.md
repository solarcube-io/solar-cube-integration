# Installing from a tar archive

For putting the integration onto a device that cannot reach HACS — an appliance
on a closed network, or a first install before HACS is configured.

## Build the archive

On a machine with the repository checked out:

```bash
./scripts/build_release_archive.sh
```

```
Built dist/solar_cube-0.2.2.tar.gz
  version : 0.2.2
  commit  : ce377f3
  files   : 25
  size    : 660K
  sha256  : 61c70d7da7fa0c543bc3f038638e00cef5047b69bc031b5ab6d56d7abb492599
```

The file list comes from `git ls-files`, so tests, previews, dev tooling and
`__pycache__` cannot end up in it. The script refuses to build if
`custom_components/solar_cube/` has uncommitted or untracked files, because
either would mean the archive does not match the commit it claims to be.

The archive contains exactly `custom_components/solar_cube/`, so it extracts
directly into a Home Assistant configuration directory.

## Copy it to the device

```bash
scp dist/solar_cube-0.2.2.tar.gz \
    dist/solar_cube-0.2.2.tar.gz.sha256 \
    sc-admin@solarcube:/tmp/
```

Verify the transfer before installing:

```bash
ssh sc-admin@solarcube 'cd /tmp && sha256sum -c solar_cube-0.2.2.tar.gz.sha256'
# solar_cube-0.2.2.tar.gz: OK
```

## Install

Pick the case that matches how Home Assistant runs on the device.

### A. Home Assistant in a container, config on a host path

The usual appliance layout. Find the host path backing `/config`:

```bash
docker inspect homeassistant \
  --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
# /opt/homeassistant/config -> /config
```

Then extract into it:

```bash
CONFIG=/opt/homeassistant/config
sudo tar -xzf /tmp/solar_cube-0.2.2.tar.gz -C "$CONFIG"
sudo ls -l "$CONFIG/custom_components/solar_cube/manifest.json"
```

### B. Home Assistant in a container, config in a named volume

No host path to write to, so copy through the container:

```bash
docker cp /tmp/solar_cube-0.2.2.tar.gz homeassistant:/tmp/
docker exec homeassistant tar -xzf /tmp/solar_cube-0.2.2.tar.gz -C /config
docker exec homeassistant rm /tmp/solar_cube-0.2.2.tar.gz
```

Under Docker-in-Docker, run these inside the outer container first:

```bash
docker cp /tmp/solar_cube-0.2.2.tar.gz docker-client:/tmp/
docker exec docker-client docker cp /tmp/solar_cube-0.2.2.tar.gz homeassistant:/tmp/
docker exec docker-client docker exec homeassistant \
  tar -xzf /tmp/solar_cube-0.2.2.tar.gz -C /config
```

### C. Home Assistant OS / Supervised

Use the Samba or SSH add-on and extract into `/config`:

```bash
tar -xzf /tmp/solar_cube-0.2.2.tar.gz -C /config
```

## Restart and add the integration

```bash
docker restart homeassistant        # or: ha core restart
```

Then in the web UI:

1. **Settings → Devices & Services → Add Integration → Solar Cube**
2. Fill in the InfluxDB URL, token, organization and bucket names.
3. Leave **Import dashboards** and **Automatically download the required
   dashboard cards** enabled unless you intend to manage those yourself.
4. When Home Assistant asks to restart (Settings → Repairs), accept — some
   Lovelace pieces only settle after one.
5. Hard-refresh the browser afterwards (Ctrl+F5 / Cmd+Shift+R) so the new
   frontend resources load.

## Upgrading an existing install

Extracting over the top is enough — the archive replaces every shipped file:

```bash
sudo tar -xzf /tmp/solar_cube-0.2.2.tar.gz -C "$CONFIG"
docker restart homeassistant
```

Your configuration is untouched: it lives in the config entry, not in these
files. If a file was removed between versions it will linger, so for a clean
upgrade delete the directory first — this discards nothing you configured:

```bash
sudo rm -rf "$CONFIG/custom_components/solar_cube"
sudo tar -xzf /tmp/solar_cube-0.2.2.tar.gz -C "$CONFIG"
```

## Getting updated dashboards after an upgrade

Dashboards are seeded once and then belong to you, so Home Assistant will not
replace a copy you might have edited. On upgrade the integration compares the
shipped dashboard against what it last wrote:

| Situation | What happens |
| --- | --- |
| Dashboard missing | created and seeded |
| Shipped copy unchanged | nothing |
| Shipped copy newer, yours untouched | refreshed automatically |
| Shipped copy newer, yours edited | left alone, and a Repairs issue explains how to take the new one |

### Upgrading from v0.1.0

v0.1.0 shipped the dashboard YAML at the top level of the repository, and its
install steps had you copy it into `/config/dashboards`. Those files are no
longer used — the dashboards now come from inside the integration — but a device
that went through v0.1.0 still has them. Delete them once:

```bash
sudo rm -rf "$CONFIG/dashboards"
```

Leaving them is harmless from this release on, and the log names each file it
ignores, but removing them avoids confusion later.

To force the shipped version and discard local edits — including on installs
that predate this behaviour — go to **Settings → Devices & Services → Solar Cube
→ Configure**, tick **Re-apply shipped dashboards**, submit, and restart when
asked. The tick is one-shot and clears itself.

That also re-applies the Energy dashboard configuration and updates the shipped
automations by id, leaving your own automations untouched.

## Checking it loaded

```bash
docker logs homeassistant 2>&1 | grep -i solar_cube | tail -20
```

A healthy start shows the setup running and, if the LCD is enabled, a line like:

```
Solar Cube S1 LCD display activated (bridge=http://solar_lcd_bridge:8765, lang=pl, currency=PLN)
```

If the integration does not appear in the Add Integration list:

- confirm `manifest.json` sits at
  `<config>/custom_components/solar_cube/manifest.json` — one directory too deep
  is the usual cause;
- confirm Home Assistant restarted after the files were placed;
- check the log for `custom integration` warnings, which are expected and
  harmless, versus an ImportError, which is not.

## What the archive does not contain

The Solar Cube PRO S1 LCD panel is driven by a separate service, the **Solar
LCD Bridge**, distributed as a Docker image rather than in this archive. It is
only needed if you enable the LCD display option. See that project's README:
<https://dev.azure.com/roygard/Solar%20Cube%20(Technology)/_git/Solar_LCD_Bridge>
