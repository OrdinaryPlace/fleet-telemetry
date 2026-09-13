#!/usr/bin/env python3
"""Build a reproducible app context from committed, reviewed runtime files."""

from __future__ import annotations

import argparse
from pathlib import Path
import stat
import subprocess


APP_FILES = {
    "receiver": ("Dockerfile", "config.yaml", "runtime.py", "archive_web.py"),
    "commissioner": ("Dockerfile", "config.yaml", "commissioner.py", "entrypoint.py", "ha_support.py"),
}
BRIDGE_FILES = ("bridge.py", "requirements.txt")


class BuildContextError(ValueError):
    """A fixed diagnostic that never contains a local file's contents."""


def git(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", *arguments], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise BuildContextError("Git source inspection failed")
    return result.stdout


def regular_source(root: Path, relative: str, mode: str) -> Path:
    """Reject symlinks, submodules, missing files, and paths outside the source."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or mode not in {"100644", "100755"}:
        raise BuildContextError("Source must contain only ordinary tracked files")
    source = root / path
    if not source.resolve().is_relative_to(root):
        raise BuildContextError("Source path escapes the repository")
    current = root
    try:
        for part in path.parts:
            current /= part
            if current.is_symlink():
                raise BuildContextError("Source symlinks are not permitted")
        if not stat.S_ISREG(source.lstat().st_mode):
            raise BuildContextError("Source is not a regular file")
    except OSError:
        raise BuildContextError("Required source file is unavailable") from None
    return source


def build_context(root: Path, destination: Path, kind: str = "receiver") -> str:
    if kind not in APP_FILES:
        raise BuildContextError("Unknown app kind")
    root = root.resolve()
    # A preexisting dangling symlink must not become an output path either.
    if destination.exists() or destination.is_symlink():
        raise BuildContextError("Destination must not exist")
    destination = destination.resolve()
    if destination.is_relative_to(root):
        raise BuildContextError("Build destination must be outside the source repository")
    revision = git(root, "rev-parse", "HEAD").decode().strip()
    tree = {}
    for item in git(root, "ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if not item:
            continue
        metadata, name = item.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode().split()
        tree[name.decode()] = (mode, object_type, object_id)
    app_prefix = "homeassistant/addon" if kind == "receiver" else "homeassistant/commissioner"
    selected = {f"{app_prefix}/{name}": Path(name) for name in APP_FILES[kind]}
    if kind == "receiver":
        selected.update({f"homeassistant/bridge/{name}": Path("bridge") / name for name in BRIDGE_FILES})
        for relative in tree:
            if not relative.startswith(("homeassistant/", ".git", "test/")):
                selected[relative] = Path("receiver") / relative
    for relative in selected:
        if relative not in tree or tree[relative][1] != "blob":
            raise BuildContextError("Required runtime files must be committed first")
        regular_source(root, relative, tree[relative][0])
    clean = subprocess.run(["git", "diff", "--quiet", revision, "--", *selected], cwd=root,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if clean.returncode:
        raise BuildContextError("Selected runtime files must match the reviewed commit")
    # Read committed blobs rather than the working tree: an ignored local file or
    # a concurrent worktree replacement cannot enter the image through a copy.
    destination.mkdir(parents=True, mode=0o700)
    for relative, target_relative in selected.items():
        mode, _, object_id = tree[relative]
        target = destination / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git(root, "cat-file", "blob", object_id))
        target.chmod(0o755 if mode == "100755" else 0o644)
    (destination / "SOURCE_REVISION").write_text(revision + "\n")
    return revision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--kind", choices=tuple(APP_FILES), default="receiver")
    args = parser.parse_args()
    try:
        build_context(Path(__file__).resolve().parents[2], args.destination, args.kind)
    except BuildContextError as error:
        print(str(error))
        return 1
    print(f"Prepared {args.kind} app context at {args.destination.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
