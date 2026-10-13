"""Windows offline-demo installer and localhost launcher; standard library only."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8000/"
FREE_BYTES_FOR_CHROMIUM = 1_000_000_000


def _run(*args: str, quiet: bool = False) -> bool:
    try:
        result = subprocess.run(
            (sys.executable, *args), cwd=ROOT, check=False,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.DEVNULL if quiet else None,
        )
        return result.returncode == 0
    except OSError:
        return False


def ensure_dependencies() -> bool:
    print("[2/3] Checking Python packages...", flush=True)
    if not _run("-m", "pip", "--version", quiet=True):
        print("pip is missing from the local environment. Trying Python's bundled ensurepip...")
        if not _run("-m", "ensurepip", "--upgrade"):
            print("[ERROR] Could not initialize pip inside .venv. Check folder permissions and Python's venv installation.")
            return False
    if _run("-m", "pip", "install", "--dry-run", "--no-index", "-r", "requirements.txt", quiet=True):
        print("Required packages are already installed; no download needed.")
        return True
    print("Some required packages are missing or out of range. Installing from requirements.txt...")
    print("This first-time step requires internet access. No system Python packages are changed.")
    if not _run("-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"):
        print("[ERROR] Dependency installation failed. Check internet access, free disk space, and .venv write permission.")
        return False
    if not _run("-m", "pip", "install", "--dry-run", "--no-index", "-r", "requirements.txt", quiet=True):
        print("[ERROR] Required packages are still unavailable after pip completed.")
        return False
    return True


def _browser_cache() -> Path:
    configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if configured == "0":
        return ROOT / ".venv"
    if configured and configured != "0":
        return Path(configured).expanduser().resolve()
    return Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright"


def _disk_free(path: Path) -> int:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return shutil.disk_usage(existing).free


def ensure_chromium() -> bool:
    print("[3/3] Checking Playwright Chromium...", flush=True)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            try:
                browser = playwright.chromium.launch()
                browser.close()
                print("Chromium is installed and starts successfully; no browser download needed.")
                return True
            except Exception as exc:
                if executable.is_file() and "Executable doesn't exist" not in str(exc):
                    print(f"[ERROR] Chromium exists but cannot start: {type(exc).__name__}: {exc}")
                    print("Check browser permissions, antivirus restrictions, and Playwright installation.")
                    return False
    except Exception as exc:
        print(f"[ERROR] Playwright could not be checked: {type(exc).__name__}: {exc}")
        print("Verify that the local package installation completed successfully.")
        return False

    cache = _browser_cache()
    try:
        free = _disk_free(cache)
    except OSError as exc:
        print(f"[ERROR] Could not check free space at {cache}: {exc}")
        print("Check the browser-cache path and drive permissions.")
        return False
    print("Chromium is not installed for this Playwright version.")
    print(f"Browser location: {cache}")
    print(f"Free space on that drive: {free / 1_000_000_000:.1f} GB.")
    print("Allow at least 1 GB free. The browser download requires internet access and may use several hundred MB.")
    if free < FREE_BYTES_FOR_CHROMIUM:
        print("[ERROR] Not enough free space for a reliable browser installation. Free space and rerun run.bat.")
        return False
    try:
        consent = input("Install Playwright Chromium now? [y/N]: ").strip().lower()
    except EOFError:
        consent = ""
    if consent not in ("y", "yes"):
        print("[ERROR] Chromium installation was declined. Rerun run.bat when ready.")
        return False
    if not _run("-m", "playwright", "install", "chromium"):
        print("[ERROR] Chromium installation failed. Check internet access, disk space, and browser-cache permission.")
        return False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            browser.close()
    except Exception as exc:
        print(f"[ERROR] Chromium was downloaded but could not start: {type(exc).__name__}: {exc}")
        return False
    return True


def _port_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.5):
            return False
    except OSError:
        return True


def start_server() -> int:
    if not _port_available():
        print("[ERROR] Local port 8000 is already in use. Stop the other server and rerun run.bat.")
        return 1
    command = (sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000")
    try:
        server = subprocess.Popen(command, cwd=ROOT)
    except OSError as exc:
        print(f"[ERROR] Could not start the local server: {exc}")
        return 1
    try:
        deadline = time.monotonic() + 20
        direct_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            if server.poll() is not None:
                print("[ERROR] Server exited during startup. Read the Uvicorn error above.")
                return 1
            try:
                with direct_http.open(URL + "api/health", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        else:
            print("[ERROR] Server did not become ready on localhost within 20 seconds.")
            return 1
        print(f"PROOF is ready at {URL}")
        try:
            if not webbrowser.open(URL):
                os.startfile(URL)
        except (OSError, AttributeError):
            print(f"Open this address manually: {URL}")
        print("Keep this window open. Press Ctrl+C to stop PROOF.", flush=True)
        return server.wait()
    except KeyboardInterrupt:
        print("Stopping PROOF...")
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def main() -> int:
    if os.name != "nt":
        print("[ERROR] This launcher is for Windows only.")
        return 1
    if not ensure_dependencies() or not ensure_chromium():
        return 1
    return start_server()


if __name__ == "__main__":
    raise SystemExit(main())
