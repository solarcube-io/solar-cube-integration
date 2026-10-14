"""The version we claim to support must be the version we test against.

hacs.json advertises a minimum Home Assistant core, and requirements-test.txt
decides which core the suite actually runs on. When those drift, CI proves
nothing about the version users are told to run -- which is how three CI
failures reached a pull request: the suite was pinned to 2025.12.0 while the
integration was deployed on 2026.1.3, and the pinned core had a dependency
break the supported one does not.
"""
from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "solar_cube"


def declared_minimum() -> str:
    return json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))["homeassistant"]


def test_the_test_plugin_stays_pinned() -> None:
    """It is what selects the core version, so an unpinned range would make the
    tested version drift silently with each new release."""
    assert re.search(
        r"^pytest-homeassistant-custom-component==\S+$",
        (ROOT / "requirements-test.txt").read_text(encoding="utf-8"),
        re.M,
    )


def test_the_suite_runs_on_the_declared_minimum_core() -> None:
    """Enforced under CI, where the environment comes from
    requirements-test.txt and this comparison is meaningful. Skipped elsewhere:
    a hand-assembled local environment drifting is a local matter, and failing
    on it would just train people to ignore the result.

    To reproduce CI locally:

        python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
    """
    installed = metadata.version("homeassistant")
    if not os.environ.get("CI"):
        if installed != declared_minimum():
            pytest.skip(
                f"local environment has Home Assistant {installed}, not the "
                f"declared minimum {declared_minimum()}"
            )
        return

    assert installed == declared_minimum(), (
        f"CI installed Home Assistant {installed} but hacs.json advertises "
        f"{declared_minimum()} as the minimum. Repin "
        "pytest-homeassistant-custom-component, or change the advertised "
        "minimum -- testing a version users are not told to run proves nothing."
    )


def test_the_manifest_and_readme_agree_on_the_minimum() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert declared_minimum() in readme, (
        f"README does not mention the minimum core {declared_minimum()} "
        "declared in hacs.json"
    )


def test_influxdb_client_is_a_minimum_and_the_suite_runs_on_it() -> None:
    """Home Assistant ships influxdb-client itself, and a custom integration
    shares its environment, so an exact pin conflicts as soon as Home Assistant
    moves to another release; hassfest rejects one outright. The manifest names a
    minimum instead, and the suite pins that same minimum: installs that already
    have it keep it, so it is the oldest version that can run in the field."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    (requirement,) = (
        r for r in manifest["requirements"] if r.startswith("influxdb-client")
    )
    match = re.fullmatch(r"influxdb-client>=(\S+)", requirement)
    assert match, f"{requirement}: name a minimum version, not a pin"
    assert re.search(
        rf"^influxdb-client=={re.escape(match.group(1))}$",
        (ROOT / "requirements-test.txt").read_text(encoding="utf-8"),
        re.M,
    ), f"requirements-test.txt must pin influxdb-client to {match.group(1)}"


def test_the_manifest_version_is_a_release_number() -> None:
    """Home Assistant keys its translation cache for custom integrations on this
    value, so a release that changes translations without bumping it renders raw
    keys in the UI."""
    version = json.loads(
        (COMPONENT / "manifest.json").read_text(encoding="utf-8")
    )["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version


# Deliberately not checked: that RELEASE_NOTES/v<version>.md exists. .gitignore
# keeps release notes out of the repository ("drafted locally"), so CI never
# sees them and such a test can only ever fail there.

