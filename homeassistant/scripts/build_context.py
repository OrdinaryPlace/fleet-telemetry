#!/usr/bin/env python3
"""Build a local HA app context from this checkout, without runtime data or Git metadata."""
from pathlib import Path
import argparse
import shutil
import subprocess

root = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("destination", type=Path)
parser.add_argument("--kind", choices=["receiver", "commissioner"], default="receiver")
args = parser.parse_args()
destination = args.destination.resolve()
if destination.exists():
    raise SystemExit("Destination must not exist")
destination.mkdir(parents=True, mode=0o700)
app_source = root / ("homeassistant/addon" if args.kind == "receiver" else "homeassistant/commissioner")
for item in app_source.iterdir():
    if item.is_file() and not item.name.startswith("."):
        shutil.copy2(item, destination / item.name)
if args.kind == "commissioner":
    print(f"Prepared one-time commissioning app at {destination}")
    raise SystemExit(0)
shutil.copytree(root / "homeassistant/bridge", destination / "bridge", ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv"))
receiver = destination / "receiver"
receiver.mkdir()
tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
for relative in tracked:
    if not relative or relative.startswith(("homeassistant/", ".git", "test/")):
        continue
    source = root / relative
    if source.is_file():
        target = receiver / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
(destination / "SOURCE_REVISION").write_text(revision + "\n")
print(f"Prepared local app build context at {destination}")
