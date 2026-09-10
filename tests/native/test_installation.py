# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Install the current wheel through the public entry points with real uv and Python."""

from __future__ import annotations

import email
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
WHEEL = os.environ.get("POWERCONTEXT_INSTALL_WHEEL")
pytestmark = pytest.mark.skipif(
    not WHEEL, reason="set POWERCONTEXT_INSTALL_WHEEL to a built wheel to run network installs"
)


def install_environment(root: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("UV_", "PIP_", "PYTHON", "POWERCONTEXT_")) and key != "VIRTUAL_ENV"
    }
    home_dir = root / "home"
    home_dir.mkdir()
    environment.update(
        HOME=str(home_dir),
        USERPROFILE=str(home_dir),
        XDG_CONFIG_HOME=str(home_dir / ".config"),
        XDG_CONFIG_DIRS=str(home_dir / "system-config"),
        UV_TOOL_DIR=str(root / "tools"),
        UV_TOOL_BIN_DIR=str(root / "bin"),
        UV_CACHE_DIR=str(root / "cache"),
        UV_PYTHON_INSTALL_DIR=str(root / "python"),
        UV_DEFAULT_INDEX="https://pypi.org/simple",
        UV_NO_PROGRESS="1",
        POWERCONTEXT_HOME=str(root / "state"),
    )
    config = root / "uv.toml"
    config.write_text("")
    environment["UV_CONFIG_FILE"] = str(config)
    assert WHEEL is not None
    wheel = Path(WHEEL).resolve()
    assert wheel.is_file(), wheel
    constraints = root / "constraints.txt"
    # A direct URL constraint ensures the script installs this commit, even if its version is already on PyPI.
    checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
    constraints.write_text(f"powercontext @ {wheel.as_uri()}#sha256={checksum}\n")
    environment["UV_CONSTRAINT"] = str(constraints)
    return environment


def run_command(command: list[str], root: Path, environment: dict[str, str], name: str) -> str:
    started = time.monotonic()
    with (root / f"{name}.log").open("w") as stream:
        result = subprocess.run(
            command,
            env=environment,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=300,
            check=False,
        )
    output = (root / f"{name}.log").read_text(errors="replace")
    print(f"{name}: {time.monotonic() - started:.2f}s")
    assert result.returncode == 0, output
    return output


def wheel_version() -> str:
    assert WHEEL is not None
    with zipfile.ZipFile(WHEEL) as archive:
        metadata_path = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_path))
    return str(metadata["Version"])


def verify_installation(root: Path, environment: dict[str, str]) -> None:
    executable = root / "bin" / ("powercontext.exe" if sys.platform == "win32" else "powercontext")
    assert run_command([str(executable), "--version"], root, environment, "version").strip() == wheel_version()
    run_command([str(executable), "server", "--help"], root, environment, "server-help")
    interpreter = root / "tools/powercontext" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    provenance = run_command(
        [
            str(interpreter),
            "-c",
            "from importlib.metadata import distribution; print(distribution('powercontext').read_text('direct_url.json'))",
        ],
        root,
        environment,
        "wheel-provenance",
    )
    assert WHEEL is not None
    assert json.loads(provenance)["url"].partition("#")[0] == Path(WHEEL).resolve().as_uri()


@pytest.mark.skipif(sys.platform == "win32", reason="the Bash installer supports macOS and Linux")
@pytest.mark.parametrize("available", ["neither", "uv", "python", "both"])
def test_bash_installer_bootstraps_only_missing_components(tmp_path: Path, available: str) -> None:
    environment = install_environment(tmp_path)
    commands = tmp_path / "commands"
    commands.mkdir()
    # Hide the runner's Python and uv while retaining the OS tools used by the official uv installer.
    for name in (
        "sh",
        "curl",
        "dirname",
        "basename",
        "uname",
        "mktemp",
        "rm",
        "grep",
        "mkdir",
        "cp",
        "mv",
        "cat",
        "sed",
        "cut",
        "head",
        "tail",
        "tar",
        "gzip",
        "sha256sum",
        "shasum",
        "chmod",
        "touch",
        "tr",
        "awk",
        "wc",
        "dd",
        "expr",
        "ls",
        "id",
        "ldd",
        "readlink",
        "sort",
        "env",
        "install",
        "sysctl",
        "xattr",
    ):
        executable = shutil.which(name)
        if executable:
            (commands / name).symlink_to(executable)
    has_uv = available in ("uv", "both")
    has_python = available in ("python", "both")
    uv = shutil.which("uv")
    assert uv is not None
    if has_uv:
        (commands / "uv").symlink_to(uv)
    if has_python:
        (commands / "python3").symlink_to(Path(sys.executable).resolve())
        environment["UV_PYTHON_DOWNLOADS"] = "never"
    environment["PATH"] = str(commands)
    preserved = Path(environment["HOME"]) / ".env"
    preserved.write_text("existing user configuration\n")
    output = run_command(
        ["/bin/bash", str(ROOT / "website/public/install.sh"), "--no-hosts", "--version", wheel_version()],
        tmp_path,
        environment,
        "install",
    )
    assert (Path(environment["HOME"]) / ".local/bin/uv").exists() is not has_uv
    assert bool(list((tmp_path / "python").glob("cpython-*"))) is not has_python
    assert ("Using local Python:" in output) is has_python
    assert preserved.read_text() == "existing user configuration\n"
    verify_installation(tmp_path, environment)
