#!/usr/bin/env sh
set -eu

log() { printf '%s\n' "$*" >&2; }
warn() { printf '%s\n' "WARN: $*" >&2; }
err() { printf '%s\n' "ERROR: $*" >&2; }

CONFIG_DIR="${CONFIG_DIR:-/config}"
COMMUNITY_DIR="$CONFIG_DIR/www/community"
TMP_DIR="${TMPDIR:-/tmp}/solar_cube_frontend_deps"

mkdir -p "$COMMUNITY_DIR" "$TMP_DIR"

py() {
   # Prefer python3 when present.
   if command -v python3 >/dev/null 2>&1; then
      python3 "$@"
   else
      python "$@"
   fi
}

download_to() {
   url="$1"
   out="$2"

   if command -v curl >/dev/null 2>&1; then
      curl -fsSL "$url" -o "$out"
      return 0
   fi
   if command -v wget >/dev/null 2>&1; then
      wget -qO "$out" "$url"
      return 0
   fi

   # Python fallback.
   py - "$url" "$out" <<'PY'
import sys
import urllib.request

url, out = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url, headers={"User-Agent": "solar-cube-installer"})
with urllib.request.urlopen(req, timeout=60) as resp:
      data = resp.read()
with open(out, "wb") as f:
      f.write(data)
PY
}

gh_release_asset_url() {
   repo="$1"  # owner/name
   tag="$2"
   name_re="$3"
   py - "$repo" "$tag" "$name_re" <<'PY'
import json
import re
import sys
import urllib.request
import urllib.error

repo, tag, name_re = sys.argv[1], sys.argv[2], sys.argv[3]
api = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
req = urllib.request.Request(api, headers={"User-Agent": "solar-cube-installer"})
try:
   with urllib.request.urlopen(req, timeout=60) as resp:
      release = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError:
   # Tag name might differ (e.g. vX.Y.Z vs VX.Y.Z) or there might be no release.
   raise SystemExit(2)

assets = release.get("assets") or []
pattern = re.compile(name_re)

def pick(pred):
      for a in assets:
            name = a.get("name") or ""
            url = a.get("browser_download_url")
            if url and pred(name):
                  return url
      return None

# Prefer matching regex.
url = pick(lambda n: bool(pattern.search(n)))

# Fallbacks: prefer .js (or .js.gz), then .zip, then any asset.
if not url:
      url = pick(lambda n: n.endswith(".js") or n.endswith(".js.gz"))
if not url:
      url = pick(lambda n: n.endswith(".zip"))
if not url and assets:
      url = assets[0].get("browser_download_url")

if not url:
      raise SystemExit(2)

print(url)
PY
}

tag_variants() {
   tag="$1"
   # Try given tag first.
   printf '%s\n' "$tag"
   case "$tag" in
      v*)
         printf '%s\n' "V${tag#v}"
         printf '%s\n' "${tag#v}"
         ;;
      V*)
         printf '%s\n' "v${tag#V}"
         printf '%s\n' "${tag#V}"
         ;;
      *)
         printf '%s\n' "v$tag"
         printf '%s\n' "V$tag"
         ;;
   esac
}

install_tag_archive() {
   repo="$1"
   tag="$2"
   folder="$3"
   js_re="$4"

   target_dir="$COMMUNITY_DIR/$folder"
   mkdir -p "$target_dir"

   url="https://github.com/$repo/archive/refs/tags/$tag.zip"
   name="$folder-$tag.zip"
   tmp="$TMP_DIR/$name"
   log "Downloading $repo tag archive $tag"
   download_to "$url" "$tmp"

   out_guess="$target_dir/$folder.js"
   extract_best_js_from_zip "$tmp" "$js_re" "$out_guess"
   rm -f "$tmp"

   installed="$(gunzip_if_needed "$out_guess")"
   base="$(basename "$installed")"
   log "Installed: /hacsfiles/$folder/$base"
   printf '%s' "/hacsfiles/$folder/$base"
}

gh_default_branch() {
   repo="$1"
   py - "$repo" <<'PY'
import json
import sys
import urllib.request

repo = sys.argv[1]
api = f"https://api.github.com/repos/{repo}"
req = urllib.request.Request(api, headers={"User-Agent": "solar-cube-installer"})
with urllib.request.urlopen(req, timeout=60) as resp:
      data = json.loads(resp.read().decode("utf-8"))
print(data.get("default_branch") or "main")
PY
}

extract_best_js_from_zip() {
   zip_path="$1"
   name_re="$2"
   out_path="$3"
   py - "$zip_path" "$name_re" "$out_path" <<'PY'
import re
import sys
import zipfile

zip_path, name_re, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
pattern = re.compile(name_re)

with zipfile.ZipFile(zip_path) as z:
      names = z.namelist()
      js = [n for n in names if n.lower().endswith(".js")]
      js_gz = [n for n in names if n.lower().endswith(".js.gz")]

      def pick(candidates):
            for n in candidates:
                  base = n.rsplit("/", 1)[-1]
                  if pattern.search(base) or pattern.search(n):
                        return n
            return candidates[0] if candidates else None

      chosen = pick(js) or pick(js_gz)
      if not chosen:
            raise SystemExit(3)

      data = z.read(chosen)
      # If gz, keep as-is; resource can point to .js.gz only if server serves it correctly.
      # Prefer writing with the same extension as chosen.
      with open(out_path, "wb") as f:
            f.write(data)
PY
}

gunzip_if_needed() {
   path="$1"
   if echo "$path" | grep -q '\.gz$'; then
      out="${path%\.gz}"
      if command -v gunzip >/dev/null 2>&1; then
         cp "$path" "$TMP_DIR/_tmp.gz"
         gunzip -f "$TMP_DIR/_tmp.gz"
         mv "$TMP_DIR/_tmp" "$out"
      else
         py - "$path" "$out" <<'PY'
import gzip
import sys

src, dst = sys.argv[1], sys.argv[2]
with gzip.open(src, "rb") as fsrc:
      data = fsrc.read()
with open(dst, "wb") as fdst:
      fdst.write(data)
PY
      fi
      rm -f "$path"
      printf '%s' "$out"
      return 0
   fi
   printf '%s' "$path"
}

install_release() {
   repo="$1"      # owner/name
   tag="$2"       # vX.Y.Z
   folder="$3"    # install folder name
   js_re="$4"     # regex for JS asset

   target_dir="$COMMUNITY_DIR/$folder"
   mkdir -p "$target_dir"

   url=""
   chosen_tag=""
   for t in $(tag_variants "$tag"); do
      url="$(gh_release_asset_url "$repo" "$t" "$js_re" 2>/dev/null || true)"
      if [ -n "$url" ]; then
         chosen_tag="$t"
         break
      fi
   done

   if [ -z "$url" ]; then
      # No release assets (or no GitHub Release). Fall back to the tag archive zip.
      for t in $(tag_variants "$tag"); do
         if install_tag_archive "$repo" "$t" "$folder" "$js_re" >/dev/null 2>&1; then
            # Re-run to print the installed URL.
            install_tag_archive "$repo" "$t" "$folder" "$js_re"
            return 0
         fi
      done

      err "No release asset URL found for $repo@$tag and tag archive fallback failed"
      return 1
   fi

   name="$(printf '%s' "$url" | sed 's#.*/##')"
   tmp="$TMP_DIR/$folder-${chosen_tag:-$tag}-$name"
   log "Downloading $repo@${chosen_tag:-$tag} → $name"
   download_to "$url" "$tmp"

   installed=""
   case "$name" in
      *.zip)
         # Extract the best matching JS.
         out_guess="$target_dir/$folder.js"
         extract_best_js_from_zip "$tmp" "$js_re" "$out_guess"
         rm -f "$tmp"
         installed="$out_guess"
         ;;
      *.js|*.js.gz)
         out="$target_dir/$name"
         mv "$tmp" "$out"
         installed="$out"
         ;;
      *)
         warn "Unknown asset type for $repo@$tag ($name); saving raw file"
         out="$target_dir/$name"
         mv "$tmp" "$out"
         installed="$out"
         ;;
   esac

   installed="$(gunzip_if_needed "$installed")"
   base="$(basename "$installed")"

   log "Installed: /hacsfiles/$folder/$base"
   printf '%s' "/hacsfiles/$folder/$base"
}

install_repo_archive() {
   repo="$1"      # owner/name
   folder="$2"    # install folder name
   js_re="$3"     # regex for JS asset

   branch="$(gh_default_branch "$repo" || echo main)"
   url="https://github.com/$repo/archive/refs/heads/$branch.zip"

   target_dir="$COMMUNITY_DIR/$folder"
   mkdir -p "$target_dir"

   name="$folder-$branch.zip"
   tmp="$TMP_DIR/$name"
   log "Downloading $repo@$branch archive"
   download_to "$url" "$tmp"

   out_guess="$target_dir/$folder.js"
   extract_best_js_from_zip "$tmp" "$js_re" "$out_guess"
   rm -f "$tmp"

   installed="$(gunzip_if_needed "$out_guess")"
   base="$(basename "$installed")"
   log "Installed: /hacsfiles/$folder/$base"
   printf '%s' "/hacsfiles/$folder/$base"
}

failures=0
resources=""
resource_urls=""

add_resource() {
   url="$1"
   if [ -n "$url" ]; then
      resources="${resources}\n- ${url}"
      if [ -n "$resource_urls" ]; then
         # IMPORTANT: use a real newline between URLs.
         # Using "\n" would store a literal backslash+n which Python won't split with splitlines().
         resource_urls="${resource_urls}
${url}"
      else
         resource_urls="$url"
      fi
   fi
}

log "Solar Cube: installing pinned frontend dependencies into $COMMUNITY_DIR"

url="$(install_release "kalkih/mini-graph-card" "v0.13.0" "mini-graph-card" 'mini-graph-card.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_release "flixlix/power-flow-card-plus" "v0.2.6" "power-flow-card-plus" 'power-flow-card-plus.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_release "rejuvenate/lovelace-horizon-card" "v1.4.0" "lovelace-horizon-card" '(lovelace-horizon-card|horizon).*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_release "totaldebug/atomic-calendar-revive" "v10.0.0" "atomic-calendar-revive" 'atomic-calendar-revive.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_release "mlamberts78/weather-chart-card" "V2.4.11" "weather-chart-card" 'weather-chart-card.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_release "flixlix/energy-flow-card-plus" "v0.1.2.1" "energy-flow-card-plus" 'energy-flow-card-plus.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_release "SpangleLabs/history-explorer-card" "v1.0.54" "history-explorer-card" 'history-explorer-card.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_repo_archive "hulkhaugen/hass-bha-icons" "hass-bha-icons" '(bha|hass-bha).*icons.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_release "MrBartusek/MeteoalarmCard" "v2.7.2" "meteoalarm-card" 'meteoalarm.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_release "flixlix/energy-period-selector-plus" "v0.2.3" "energy-period-selector-plus" 'energy-period-selector-plus.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_release "zeronounours/lovelace-energy-entity-row" "v1.2.0" "energy-entity-row" 'energy-entity-row.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

url="$(install_release "RomRider/apexcharts-card" "v2.2.3" "apexcharts-card" 'apexcharts-card.*\\.js(\\.gz)?$')" || { failures=$((failures+1)); url=""; }
add_resource "$url"

log ""
log "Next steps (manual):"
log "- Home Assistant → Settings → Dashboards → Resources → Add resource"
log "- Type: JavaScript module"
log "- Add these URLs:"
printf '%b\n' "$resources" >&2
log "- Reload the browser page (hard refresh) after adding resources"

# Best-effort automation: update HA storage directly.
# NOTE: Home Assistant may overwrite this file while running; this is not an official/public API.
storage_file=""
storage_dir="$CONFIG_DIR/.storage"

is_lovelace_yaml_mode() {
   cfg="$CONFIG_DIR/configuration.yaml"
   [ -f "$cfg" ] || return 1
   # Extremely simple detection: good enough for user messaging.
   py - "$cfg" <<'PY'
import re
import sys

path = sys.argv[1]
try:
   text = open(path, 'r', encoding='utf-8').read()
except OSError:
   raise SystemExit(1)

# Detect a top-level-ish lovelace: with mode: yaml.
pattern = re.compile(r"(?ms)^lovelace\s*:\s*\n(?:^\s+.*\n)*?^\s*mode\s*:\s*yaml\s*$")
raise SystemExit(0 if pattern.search(text) else 1)
PY
}

ensure_storage_resources_file() {
   if [ ! -d "$storage_dir" ]; then
      mkdir -p "$storage_dir" 2>/dev/null || return 1
   fi
   if [ -f "$storage_dir/lovelace_resources" ]; then
      return 0
   fi

   # If Lovelace is running in YAML mode, .storage resources may not be used.
   if is_lovelace_yaml_mode; then
      return 2
   fi

   # Create a minimal Store-compatible skeleton.
    tmp="$TMP_DIR/lovelace_resources.new"
   cat >"$tmp" <<'JSON'
{
  "version": 1,
   "minor_version": 1,
   "key": "lovelace_resources",
  "data": {
    "items": []
  }
}
JSON
    mv "$tmp" "$storage_dir/lovelace_resources"
   return 0
}

if ensure_storage_resources_file; then
   storage_file="$storage_dir/lovelace_resources"
else
   rc=$?
   if [ "$rc" -eq 2 ]; then
      warn "Lovelace appears to be in YAML mode (configuration.yaml: lovelace: mode: yaml)."
      warn "Dashboard Resources are then configured in ui-lovelace YAML, not /config/.storage/lovelace_resources."
   fi
fi

if [ -n "$storage_file" ] && [ -n "$resource_urls" ]; then
   log ""
   log "Attempting to auto-add resources in: $storage_file"
   if RESOURCE_URLS="$resource_urls" py - "$storage_file" <<'PY'
import json
import os
import sys
import tempfile
import time
import uuid

storage_path = sys.argv[1]
raw_urls_env = os.environ.get("RESOURCE_URLS", "")
raw_urls_env = raw_urls_env.replace("\\n", "\n")
urls = [line.strip() for line in raw_urls_env.splitlines() if line.strip()]

with open(storage_path, "r", encoding="utf-8") as f:
   raw = f.read()
data = json.loads(raw) if raw.strip() else {}

items = None
container = None
items_key = None

if isinstance(data, dict):
   d = data.get("data")
   if isinstance(d, dict) and isinstance(d.get("items"), list):
      container = d
      items_key = "items"
      items = d["items"]
   elif isinstance(d, list):
      container = data
      items_key = "data"
      items = d
   elif isinstance(data.get("items"), list):
      container = data
      items_key = "items"
      items = data["items"]

if items is None:
   raise SystemExit("Unsupported lovelace_resources format; leaving manual steps")

existing_urls = set()
existing_ids_int = []

def split_maybe_multi_url(value: str) -> list[str]:
   # Handle both real newlines and literal "\\n" sequences.
   text = value.replace("\\n", "\n")
   parts = [p.strip() for p in text.splitlines() if p.strip()]
   return parts or [value]

changed = False
repair_urls: list[str] = []

# Repair any existing broken entries where a single "url" contains multiple URLs.
repaired_items = []
for it in items:
   if not isinstance(it, dict):
      repaired_items.append(it)
      continue

   u = it.get("url")
   i = it.get("id")
   if isinstance(i, int):
      existing_ids_int.append(i)

   if isinstance(u, str) and ("\n" in u or "\\n" in u):
      parts = split_maybe_multi_url(u)
      # Drop the broken multi-url entry; we'll recreate per-URL items below.
      changed = True
      for part in parts:
         repair_urls.append(part)
      continue

   if isinstance(u, str):
      existing_urls.add(u)
   repaired_items.append(it)

items[:] = repaired_items

use_int_ids = len(existing_ids_int) > 0
next_int_id = (max(existing_ids_int) + 1) if existing_ids_int else 1

added = 0

def alloc_id():
   global next_int_id
   if use_int_ids:
      rid = next_int_id
      next_int_id += 1
      return rid
   return uuid.uuid4().hex

# First, apply repairs for URLs found inside broken entries.
for url in repair_urls:
   if url in existing_urls:
      continue
   items.append({"id": alloc_id(), "type": "module", "url": url})
   existing_urls.add(url)
   added += 1

for url in urls:
   if url in existing_urls:
      continue
   items.append({"id": alloc_id(), "type": "module", "url": url})
   existing_urls.add(url)
   added += 1

if urls and added == 0:
   # No-op; keep file as-is.
   pass

if added or changed:
   backup_path = f"{storage_path}.bak.{int(time.time())}"
   with open(backup_path, "w", encoding="utf-8") as f:
      f.write(raw)

   dir_name = os.path.dirname(storage_path)
   fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".lovelace_resources.", suffix=".tmp")
   try:
      with os.fdopen(fd, "w", encoding="utf-8") as f:
         json.dump(data, f, ensure_ascii=False, indent=2)
         f.write("\n")
      os.replace(tmp_path, storage_path)
   finally:
      try:
         if os.path.exists(tmp_path):
            os.remove(tmp_path)
      except OSError:
         pass

if changed and added:
   print(f"Repaired resources file and added {added} resource(s)")
elif changed:
   print("Repaired resources file")
else:
   print(f"Added {added} resource(s)")
PY
   then
   log "Auto-add completed. If cards still don’t load, restart Home Assistant and hard refresh the browser."
   else
   warn "Auto-add failed; keep using the manual steps above."
   fi
else
   warn "HA resource storage file not found at $CONFIG_DIR/.storage/lovelace_resources; keep using the manual steps above."
fi

if [ "$failures" -gt 0 ]; then
   err "Some downloads failed ($failures). Check network access and Home Assistant logs."
   exit 1
fi

exit 0
