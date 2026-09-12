"""Isolated fixture repositories only; no production files or credentials."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from build_context import APP_FILES, BRIDGE_FILES, BuildContextError, build_context, git as read_git


class BuildContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "source"
        self.root.mkdir()
        self.run_git("init", "-q")
        for kind, names in APP_FILES.items():
            prefix = "homeassistant/addon" if kind == "receiver" else "homeassistant/commissioner"
            for name in names:
                self.write(f"{prefix}/{name}", "synthetic committed runtime\n")
        for name in BRIDGE_FILES:
            self.write(f"homeassistant/bridge/{name}", "synthetic committed bridge\n")
        self.write("cmd/main.go", "package main\n")
        self.write("go.mod", "module example.invalid/test\n")
        self.write("go.sum", "")
        self.write(".gitignore", "*.private.json\n.env\n")
        self.write("test/integration/synthetic-fixture.txt", "public fixture excluded from runtime\n")
        self.write("homeassistant/README.md", "documentation\n")
        self.commit()

    def run_git(self, *arguments):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                                        "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid",
                                        *arguments], cwd=self.root, stderr=subprocess.DEVNULL)

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def commit(self):
        self.run_git("add", "--all")
        self.run_git("commit", "-qm", "Synthetic fixture")

    def test_allowlist_excludes_ignored_untracked_and_test_files(self):
        for relative in ("homeassistant/addon/options.private.json", "homeassistant/bridge/.env",
                         "homeassistant/bridge/untracked.json", "homeassistant/commissioner/local.private.json"):
            self.write(relative, "SYNTHETIC_PRIVATE_SENTINEL")
        # Ignored artifacts do not make the repository's ordinary status dirty.
        self.assertTrue(self.run_git("check-ignore", "homeassistant/bridge/.env").strip())
        for kind in ("receiver", "commissioner"):
            target = self.directory / kind
            revision = build_context(self.root, target, kind)
            self.assertEqual((target / "SOURCE_REVISION").read_text().strip(), revision)
            self.assertEqual(revision, self.run_git("rev-parse", "HEAD").decode().strip())
            contents = "".join(p.read_text() for p in target.rglob("*") if p.is_file())
            self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", contents)
            self.assertFalse((target / "receiver/test").exists())
            self.assertFalse((target / ".git").exists())
            expected = len(APP_FILES[kind]) + 1 + (len(BRIDGE_FILES) + 3 if kind == "receiver" else 0)
            self.assertEqual(sum(p.is_file() for p in target.rglob("*")), expected)

    def test_worktree_symlink_cannot_read_outside_source(self):
        outside = self.directory / "outside.txt"
        outside.write_text("SYNTHETIC_OUTSIDE_SENTINEL")
        source = self.root / "homeassistant/bridge/bridge.py"
        source.unlink()
        source.symlink_to(outside)
        target = self.directory / "context"
        with self.assertRaises(BuildContextError):
            build_context(self.root, target)
        self.assertFalse(target.exists())
        self.assertEqual(outside.read_text(), "SYNTHETIC_OUTSIDE_SENTINEL")

    def test_tracked_symlink_is_rejected_even_when_pointing_inside_repository(self):
        (self.root / "tracked-link").symlink_to("go.mod")
        self.commit()
        with self.assertRaises(BuildContextError):
            build_context(self.root, self.directory / "context")

    def test_symlinked_ancestor_is_rejected(self):
        original = self.root / "homeassistant/bridge"
        outside = self.directory / "bridge-copy"
        shutil.copytree(original, outside)
        shutil.rmtree(original)
        original.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(BuildContextError):
            build_context(self.root, self.directory / "context")

    def test_modified_runtime_requires_reviewed_commit(self):
        self.write("homeassistant/addon/runtime.py", "uncommitted runtime\n")
        with self.assertRaisesRegex(BuildContextError, "reviewed commit"):
            build_context(self.root, self.directory / "context")

    def test_unrelated_documentation_edit_does_not_change_runtime_provenance(self):
        self.write("homeassistant/README.md", "uncommitted documentation\n")
        revision = build_context(self.root, self.directory / "context")
        self.assertEqual(revision, self.run_git("rev-parse", "HEAD").decode().strip())

    def test_untracked_required_file_cannot_be_packaged(self):
        self.run_git("rm", "--cached", "homeassistant/addon/runtime.py")
        self.run_git("commit", "-qm", "Remove fixture runtime")
        with self.assertRaisesRegex(BuildContextError, "committed first"):
            build_context(self.root, self.directory / "context")

    def test_destination_cannot_exist_or_point_back_into_source(self):
        for target in (self.directory, self.root / "generated-context"):
            with self.assertRaises(BuildContextError):
                build_context(self.root, target)
        dangling = self.directory / "dangling"
        dangling.symlink_to(self.directory / "missing")
        with self.assertRaises(BuildContextError):
            build_context(self.root, dangling)

    def test_blob_contents_are_not_taken_from_concurrent_worktree_replacement(self):
        def replace_after_validation(root, *arguments):
            if arguments[0] == "cat-file":
                self.write("homeassistant/addon/runtime.py", "SYNTHETIC_LATE_REPLACEMENT")
            return read_git(root, *arguments)

        target = self.directory / "context"
        with patch("build_context.git", side_effect=replace_after_validation):
            build_context(self.root, target)
        self.assertEqual((self.root / "homeassistant/addon/runtime.py").read_text(), "SYNTHETIC_LATE_REPLACEMENT")
        self.assertEqual((target / "runtime.py").read_bytes(), self.run_git("show", "HEAD:homeassistant/addon/runtime.py"))


if __name__ == "__main__":
    unittest.main()
