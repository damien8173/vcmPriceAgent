#!/usr/bin/env python3
"""Cross-platform, no-Docker launcher for the HKEX Dividend Monitor.

For machines without Docker Desktop (common on locked-down Windows
setups). This script:
  1. Downloads the official SurrealDB binary for the current OS/arch
     (if not already present under .local/bin/).
  2. Downloads and pip-installs the hkex-filing-scraper package from
     source (it isn't published on PyPI -- see ensure_hkex_scraper_installed)
     if the `hkex-scraper` command isn't already on PATH.
  3. Starts SurrealDB as a subprocess with file-backed storage under
     data/surrealdb/.
  4. Starts the background monitor daemon.
  5. Starts the web dashboard (uvicorn) and opens it in your browser.

All child processes are stopped together when you press Ctrl+C or
close this window -- there is no separate "stop" step in no-Docker
mode, unlike Docker Compose, which can run detached. Keep this window
open while you're using the app.

Usage:
    Windows (PowerShell):  py scripts\\run_local.py
    macOS / Linux:         python3 scripts/run_local.py

Requires Python 3.10+ and: pip install -r requirements.txt
(camelot/ghostscript table-extraction is intentionally skipped here --
see README for why; use Docker if you need it.)
"""
from __future__ import annotations

import atexit
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_BIN_DIR = REPO_ROOT / ".local" / "bin"
LOCAL_SRC_DIR = REPO_ROOT / ".local" / "src"
SURREAL_DATA_DIR = REPO_ROOT / "data" / "surrealdb"
GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/surrealdb/surrealdb/releases/latest"
# Pinned to the same commit as the Dockerfile's HKEX_SCRAPER_COMMIT build arg
# -- this repo has no tagged releases, and always tracking main would mean
# this launcher and the Docker image could silently drift onto different,
# untested versions of the upstream scraper. Bump both together deliberately.
HKEX_SCRAPER_COMMIT = "e53df8b7e58f17b70d6750aebb589b4c710a629f"
HKEX_SCRAPER_ZIP_URL = (
    f"https://github.com/simonplmak-cloud/hkex-filing-scraper/archive/{HKEX_SCRAPER_COMMIT}.zip"
)
DASHBOARD_URL = "http://localhost:8080"

# Matches docker-compose.yml's fallback -- fine since SurrealDB only
# listens on localhost here too, never exposed to the network.
DEFAULT_SURREAL_PASSWORD = "hkex-local-dev-password"

_child_processes: list[tuple[str, subprocess.Popen]] = []


def _platform_asset_info() -> tuple[str, bool]:
    """Return (asset_suffix, is_tgz) for the current OS/arch, matching
    SurrealDB's GitHub release naming convention."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"

    if system == "windows":
        return f"windows-{arch}.exe", False
    if system == "darwin":
        return f"darwin-{arch}.tgz", True
    if system == "linux":
        return f"linux-{arch}.tgz", True
    raise RuntimeError(f"Unsupported platform: {system} ({machine})")


def _binary_path() -> Path:
    return LOCAL_BIN_DIR / ("surreal.exe" if platform.system().lower() == "windows" else "surreal")


def _resolve_latest_download_url(asset_suffix: str) -> str:
    req = urllib.request.Request(
        GITHUB_LATEST_RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "hkex-dividend-monitor"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        import json

        payload = json.load(resp)

    for asset in payload.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(asset_suffix):
            return asset["browser_download_url"]

    raise RuntimeError(f"Could not find a SurrealDB release asset matching '*{asset_suffix}'")


def ensure_surreal_binary() -> Path:
    binary_path = _binary_path()
    if binary_path.exists():
        return binary_path

    print("SurrealDB binary not found locally -- downloading for this platform...")
    LOCAL_BIN_DIR.mkdir(parents=True, exist_ok=True)

    asset_suffix, is_tgz = _platform_asset_info()
    url = _resolve_latest_download_url(asset_suffix)
    print(f"Downloading: {url}")

    if is_tgz:
        archive_path = LOCAL_BIN_DIR / "surreal.tgz"
        urllib.request.urlretrieve(url, archive_path)
        with tarfile.open(archive_path, "r:gz") as tar:
            # The archive contains a single `surreal` binary at its root.
            for member in tar.getmembers():
                if Path(member.name).name == "surreal":
                    member.name = "surreal"  # flatten any leading path
                    try:
                        tar.extract(member, path=LOCAL_BIN_DIR, filter="data")
                    except TypeError:
                        # Python < 3.12 doesn't have the `filter` kwarg.
                        tar.extract(member, path=LOCAL_BIN_DIR)
                    break
            else:
                raise RuntimeError("Downloaded archive did not contain a 'surreal' binary")
        archive_path.unlink(missing_ok=True)
    else:
        urllib.request.urlretrieve(url, binary_path)

    if not binary_path.exists():
        raise RuntimeError(f"Expected SurrealDB binary at {binary_path} after download/extract")

    if platform.system().lower() != "windows":
        binary_path.chmod(binary_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    print(f"SurrealDB binary ready at {binary_path}")
    return binary_path


def ensure_hkex_scraper_installed() -> None:
    """Install the hkex-filing-scraper package (the `hkex-scraper` CLI) if
    it isn't already available.

    This package is NOT published on PyPI -- `pip install hkex-filing-scraper`
    404s. The Dockerfile installs it via `git clone` + `pip install .[all]`;
    here we avoid a hard `git` dependency (a user who got this repo via
    GitHub's "Download ZIP" button may not have git installed at all) by
    downloading the same source as a zip via Python's own `zipfile`, then
    pip-installing it as a local directory, exactly mirroring how
    ensure_surreal_binary() fetches SurrealDB without any external tool.
    """
    if shutil.which("hkex-scraper") is not None:
        return

    print("hkex-filing-scraper not found -- installing from source (not on PyPI)...")
    LOCAL_SRC_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = LOCAL_SRC_DIR / "hkex-filing-scraper.zip"

    print(f"Downloading: {HKEX_SCRAPER_ZIP_URL}")
    urllib.request.urlretrieve(HKEX_SCRAPER_ZIP_URL, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(LOCAL_SRC_DIR)
    zip_path.unlink(missing_ok=True)

    extracted_dirs = [
        p for p in LOCAL_SRC_DIR.iterdir() if p.is_dir() and p.name.startswith("hkex-filing-scraper")
    ]
    if not extracted_dirs:
        raise RuntimeError(
            f"Downloaded zip did not contain an hkex-filing-scraper-* directory in {LOCAL_SRC_DIR}"
        )
    source_dir = extracted_dirs[0]

    print("Installing hkex-scraper (this can take a minute)...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
         f"{source_dir}[pdf,excel]"],
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to pip install hkex-filing-scraper from source")
    print("hkex-scraper installed.")


def _ensure_env_file() -> None:
    """Create .env from .env.example on first run, matching the Docker
    launchers -- no manual setup step required to get started."""
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        return
    example_file = REPO_ROOT / ".env.example"
    if not example_file.exists():
        raise RuntimeError(f".env.example not found at {example_file}")
    print("No .env file found -- creating one from the template.")
    shutil.copyfile(example_file, env_file)


def _read_surreal_password() -> str:
    """Read SURREAL_PASSWORD from .env without requiring python-dotenv to
    already be installed at this bootstrap stage. Falls back to the same
    default Docker mode uses if unset/blank."""
    env_file = REPO_ROOT / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("SURREAL_PASSWORD="):
            value = line.split("=", 1)[1].strip()
            return value or DEFAULT_SURREAL_PASSWORD
    return DEFAULT_SURREAL_PASSWORD


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("SURREAL_ENDPOINT", "http://localhost:8000")
    env.setdefault("SURREAL_PASSWORD", _read_surreal_password())
    return env


def start_surrealdb(binary_path: Path, password: str) -> subprocess.Popen:
    SURREAL_DATA_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(binary_path),
        "start",
        "--user", "root",
        "--pass", password,
        f"rocksdb:{SURREAL_DATA_DIR}",
    ]
    print(f"Starting SurrealDB: {' '.join(cmd[:-1])} <data-dir>")
    proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT))
    _child_processes.append(("SurrealDB", proc))

    # Give it a moment to bind before we proceed.
    time.sleep(2)
    if proc.poll() is not None:
        raise RuntimeError(f"SurrealDB exited immediately with code {proc.returncode}")
    return proc


def start_daemon() -> subprocess.Popen:
    print("Starting monitor daemon...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "monitor.daemon"],
        cwd=str(REPO_ROOT),
        env=_child_env(),
    )
    _child_processes.append(("monitor daemon", proc))
    return proc


def start_web_dashboard() -> subprocess.Popen:
    print("Starting web dashboard...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "monitor.web:app", "--host", "127.0.0.1", "--port", "8080"],
        cwd=str(REPO_ROOT),
        env=_child_env(),
    )
    _child_processes.append(("web dashboard", proc))
    return proc


def _stop_all() -> None:
    for name, proc in reversed(_child_processes):
        if proc.poll() is None:
            print(f"Stopping {name}...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> int:
    print("=== HKEX Dividend Monitor -- Local (no-Docker) Launcher ===")
    atexit.register(_stop_all)
    try:
        _ensure_env_file()
        password = _read_surreal_password()
        binary_path = ensure_surreal_binary()
        ensure_hkex_scraper_installed()
        start_surrealdb(binary_path, password)
        start_daemon()
        start_web_dashboard()

        time.sleep(3)
        print(f"\nDashboard: {DASHBOARD_URL}")
        try:
            webbrowser.open(DASHBOARD_URL)
        except Exception:  # noqa: BLE001 - opening a browser is best-effort
            pass
        print("\nEverything is running. Keep this window open.")
        print("Press Ctrl+C to stop.\n")

        while True:
            for name, proc in _child_processes:
                code = proc.poll()
                if code is not None:
                    print(f"\n{name} exited unexpectedly (code {code}). Stopping everything.")
                    return 1
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1
    finally:
        _stop_all()


if __name__ == "__main__":
    sys.exit(main())
