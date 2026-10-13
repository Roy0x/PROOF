"""Windows judge package and first-run guardrails, without downloads."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from scripts import build_judge_zip, judge_start


def test_judge_zip_has_only_explicit_runtime_inventory(tmp_path):
    first = build_judge_zip.build(tmp_path / "one")
    second = build_judge_zip.build(tmp_path / "two")
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    with ZipFile(first) as archive:
        names = archive.namelist()
        prefix = build_judge_zip.PACKAGE + "/"
        assert all(name.startswith(prefix) for name in names)
        relative = {name.removeprefix(prefix) for name in names}
        assert set(build_judge_zip.DOCUMENTS) <= relative
        assert {"app/main.py", "app/demo_v07.py", "app/static/workspace.html",
                "app/static/workspace.js", "app/fixtures/login_service.py.txt",
                "app/fixtures/test_login_service.py.txt"} <= relative
        assert all(not any(part in {".venv", ".git", "__pycache__", "dist", "tests", "tmp"}
                           for part in Path(name).parts) for name in names)
        assert all(not name.lower().endswith((".env", ".db", ".sqlite", ".log", ".png", ".zip"))
                   for name in names)
        assert all("screenshot" not in name.lower() and "proof-pack" not in name.lower() for name in names)
        secret = re.compile(rb"(?i)(nvapi-[a-z0-9_-]{12,}|sk-[a-z0-9_-]{20,}|ghp_[a-z0-9]{20,}|"
                            rb"github_pat_[a-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA|OPENSSH|EC|PRIVATE) KEY)")
        assert all(not secret.search(archive.read(name)) for name in names)


def test_untracked_application_file_is_excluded(tmp_path):
    canary = build_judge_zip.ROOT / "app" / f"_private_{uuid4().hex}.py"
    try:
        canary.write_text("private = True\n", encoding="utf-8")
        assert canary not in build_judge_zip.inventory()
        with ZipFile(build_judge_zip.build(tmp_path)) as archive:
            assert all(canary.name not in name for name in archive.namelist())
    finally:
        canary.unlink(missing_ok=True)


def test_launcher_skips_package_install_when_requirements_already_satisfied(monkeypatch):
    calls = []
    monkeypatch.setattr(judge_start, "_run", lambda *args, **kwargs: calls.append(args) or True)
    assert judge_start.ensure_dependencies()
    assert len(calls) == 2
    assert calls[1][:5] == ("-m", "pip", "install", "--dry-run", "--no-index")


def test_launcher_does_not_download_browser_without_consent(monkeypatch, tmp_path):
    class MissingChromium:
        executable_path = str(tmp_path / "missing-chrome.exe")
        def launch(self):
            raise RuntimeError("Executable doesn't exist")

    class Playwright:
        chromium = MissingChromium()
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("playwright.sync_api.sync_playwright", Playwright)
    monkeypatch.setattr(judge_start, "_browser_cache", lambda: tmp_path)
    monkeypatch.setattr(judge_start, "_disk_free", lambda path: 2_000_000_000)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    calls = []
    monkeypatch.setattr(judge_start, "_run", lambda *args, **kwargs: calls.append(args) or True)
    assert not judge_start.ensure_chromium()
    assert calls == []


def test_launcher_installs_only_chromium_after_consent(monkeypatch, tmp_path):
    state = {"installed": False}
    class Browser:
        def close(self):
            pass
    class Chromium:
        executable_path = str(tmp_path / "missing-chrome.exe")
        def launch(self):
            if not state["installed"]:
                raise RuntimeError("Executable doesn't exist")
            return Browser()
    class Playwright:
        chromium = Chromium()
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
    calls = []
    def install(*args, **kwargs):
        calls.append(args)
        state["installed"] = True
        return True
    monkeypatch.setattr("playwright.sync_api.sync_playwright", Playwright)
    monkeypatch.setattr(judge_start, "_browser_cache", lambda: tmp_path)
    monkeypatch.setattr(judge_start, "_disk_free", lambda path: 2_000_000_000)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(judge_start, "_run", install)
    assert judge_start.ensure_chromium()
    assert calls == [("-m", "playwright", "install", "chromium")]
