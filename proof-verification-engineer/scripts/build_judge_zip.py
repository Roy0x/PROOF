"""Build the versioned Windows offline judge ZIP from an explicit inventory."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "PROOF-v0.8-offline-windows"
ZIP_NAME = f"{PACKAGE}.zip"
DOCUMENTS = (
    "README.md", "LICENSE", "START_HERE_WINDOWS.txt", "requirements.txt", "run.bat",
    "scripts/judge_start.py", "docs/live-nemotron-v07.md",
)


def inventory() -> list[Path]:
    files = [ROOT / name for name in DOCUMENTS]
    tracked = subprocess.check_output(
        ("git", "ls-files", "--", "app"), cwd=ROOT, text=True,
    ).splitlines()
    files += [ROOT / name for name in tracked if (
        name.endswith(".py") or
        name.startswith("app/static/") and Path(name).suffix in {".html", ".css", ".js"} or
        name.startswith("app/fixtures/") and name.endswith(".txt")
    )]
    for path in files:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"Missing or unsafe package input: {path}")
    return sorted(set(files), key=lambda path: path.relative_to(ROOT).as_posix())


def build(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / ZIP_NAME
    temporary = output_dir / (ZIP_NAME + ".tmp")
    with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in inventory():
            name = f"{PACKAGE}/{path.relative_to(ROOT).as_posix()}"
            info = ZipInfo(name, date_time=(2026, 10, 13, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
    temporary.replace(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    path = build(args.output_dir.resolve())
    print(f"Created {path} ({path.stat().st_size:,} bytes; {len(inventory())} files)")
