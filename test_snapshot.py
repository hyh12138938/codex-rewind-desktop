"""Run: python -m unittest -v test_snapshot.py"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from snapshot import ConflictError, SnapshotStore


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.store = SnapshotStore(self.base / "state")

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_rollback_redo_restart_and_unrelated_manual_edits(self):
        self.write("edit.txt", b"before")
        self.write("deleted.bin", b"\x00\xffbefore")
        self.write("unrelated.txt", b"original")
        before = self.store.capture(self.root)
        self.write("edit.txt", b"after")
        (self.root / "deleted.bin").unlink()
        self.write("new/nested.bin", b"\x00\xfeafter")
        after = self.store.capture(self.root)
        self.assertEqual([c["status"] for c in self.store.diff(before, after)],
                         ["deleted", "modified", "added"])
        self.write("unrelated.txt", b"manual edit")
        self.store = SnapshotStore(self.base / "state")
        self.store.restore(self.root, before, after)
        self.assertEqual((self.root / "edit.txt").read_bytes(), b"before")
        self.assertEqual((self.root / "deleted.bin").read_bytes(), b"\x00\xffbefore")
        self.assertFalse((self.root / "new/nested.bin").exists())
        self.assertEqual((self.root / "unrelated.txt").read_bytes(), b"manual edit")
        self.store.restore(self.root, after, before)
        self.assertEqual((self.root / "edit.txt").read_bytes(), b"after")
        self.assertFalse((self.root / "deleted.bin").exists())
        self.assertEqual((self.root / "new/nested.bin").read_bytes(), b"\x00\xfeafter")

    def test_conflict_is_preflighted_before_any_write(self):
        self.write("a", b"0")
        self.write("z", b"0")
        before = self.store.capture(self.root)
        self.write("a", b"1")
        self.write("z", b"1")
        after = self.store.capture(self.root)
        self.write("z", b"manual")
        with self.assertRaises(ConflictError):
            self.store.restore(self.root, before, after)
        self.assertEqual((self.root / "a").read_bytes(), b"1")
        self.assertEqual((self.root / "z").read_bytes(), b"manual")

    def test_failure_rolls_back_applied_files(self):
        self.write("a", b"0")
        self.write("b", b"0")
        before = self.store.capture(self.root)
        self.write("a", b"1")
        self.write("b", b"1")
        after = self.store.capture(self.root)
        atomic = self.store._atomic
        calls = 0

        def fail_once(path, data, mode=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected write failure")
            return atomic(path, data, mode)

        with patch.object(self.store, "_atomic", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected"):
                self.store.restore(self.root, before, after)
        self.assertEqual((self.root / "a").read_bytes(), b"1")
        self.assertEqual((self.root / "b").read_bytes(), b"1")

    def test_exclusions_storage_and_invalid_ids(self):
        self.write("node_modules/ignore", b"large")
        self.write(".git/config", b"git")
        self.write(".venv/ignore", b"venv")
        self.write("keep", b"yes")
        identity = self.store.capture(self.root)
        self.assertEqual(set(self.store._load(identity)["files"]), {"keep"})
        with self.assertRaises(ValueError):
            self.store._load("../arbitrary")
        with self.assertRaises(ValueError):
            self.store._relative("../escape")
        with self.assertRaises(ValueError):
            self.store._relative("C:/escape")
        with self.assertRaises(ValueError):
            SnapshotStore(self.root / "state").capture(self.root)

    def test_corrupt_object_does_not_write(self):
        self.write("a", b"0")
        before = self.store.capture(self.root)
        self.write("a", b"1")
        after = self.store.capture(self.root)
        digest = self.store._load(before)["files"]["a"]["sha256"]
        (self.store.data_dir / "objects" / digest).write_bytes(b"corrupted")
        with self.assertRaises(ValueError):
            self.store.restore(self.root, before, after)
        self.assertEqual((self.root / "a").read_bytes(), b"1")

    def test_symlink_not_followed_and_restore_rejected(self):
        self.write("folder/a", b"0")
        before = self.store.capture(self.root)
        self.write("folder/a", b"1")
        after = self.store.capture(self.root)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "a").write_bytes(b"outside")
        (self.root / "folder/a").unlink()
        (self.root / "folder").rmdir()
        try:
            os.symlink(outside, self.root / "folder", target_is_directory=True)
        except OSError as error:
            if os.name != "nt":
                self.skipTest(f"Symlink privilege unavailable: {error}")
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(self.root / "folder"), str(outside)],
                capture_output=True)
            if result.returncode:
                self.skipTest(f"Symlink/junction unavailable: {result.stderr!r}")
        try:
            self.assertEqual(self.store._load(self.store.capture(self.root))["files"], {})
            with self.assertRaises(ValueError):
                self.store.restore(self.root, before, after)
            self.assertEqual((outside / "a").read_bytes(), b"outside")
        finally:
            if os.name == "nt":
                os.rmdir(self.root / "folder")
            else:
                (self.root / "folder").unlink()


if __name__ == "__main__":
    unittest.main()
