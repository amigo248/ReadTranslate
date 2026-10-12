"""Merge a staged runtime without replacing or deleting the user's data."""
from pathlib import Path
from datetime import datetime
import hashlib
import shutil
import os
import subprocess
import argparse
from assemble_standalone import long_path,copy_asset

if os.name == "nt":
    running = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "@(Get-Process -Name '阅译','阅译OCR' -ErrorAction SilentlyContinue).Count"],
        capture_output=True, text=True, check=True,
    )
    if int(running.stdout.strip() or "0"):
        raise SystemExit("Close ReadTranslate and its OCR worker before installing the update.")

ROOT = Path(__file__).resolve().parent.parent
parser=argparse.ArgumentParser()
parser.add_argument('--stage',type=Path,default=ROOT/'build/standalone-release/阅译')
args=parser.parse_args()
stage = args.stage.resolve()
target = ROOT / "dist" / "阅译"
assert stage.is_relative_to(ROOT) and target.is_relative_to(ROOT)
assert (stage / "阅译.exe").is_file() and (stage / "阅译OCR.exe").is_file()
stage=long_path(stage)
target=long_path(target)
backup = target / "data" / "backups" / ("runtime-" + datetime.now().strftime("%Y%m%d-%H%M%S"))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


changed = unchanged = 0
sources=[source for source in stage.rglob('*') if source.is_file()
         and source.relative_to(stage).parts[0] not in {'data','standalone-input'}]
for source in sources:
    relative=source.relative_to(stage)
    assert relative.parts[0] in {'_internal','engines'} or str(relative) in {'阅译.exe','阅译OCR.exe'},relative
for source in sources:
    relative = source.relative_to(stage)
    assert relative.parts[0] in {"_internal", "engines"} or str(relative) in {"阅译.exe", "阅译OCR.exe"}
    destination = target / relative
    if destination.exists() and source.stat().st_size == destination.stat().st_size and digest(source) == digest(destination):
        unchanged += 1
        continue
    if destination.exists():
        previous = backup / relative
        previous.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, previous)
    destination.parent.mkdir(parents=True, exist_ok=True)
    copy_asset(source, destination)
    assert digest(source) == digest(destination)
    changed += 1
print(f"Runtime updated: {changed} changed files, {unchanged} identical files preserved; library data untouched.")
print(f"Previous runtime files: {backup}")
