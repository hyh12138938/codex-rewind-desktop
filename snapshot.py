"""Persistent, Git-independent file snapshots for desktop rewind.

Snapshots cover regular files only. Links/junctions and excluded directories are
not followed. restore() changes only the target/expected difference, preserving
unrelated edits. Callers must stop the agent before restoring: the preflight is
not an operating-system-wide workspace lock.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile


class ConflictError(RuntimeError):
    """The workspace no longer matches the expected side of a restore."""


class SnapshotStore:
    EXCLUDED = frozenset({".git", "node_modules", ".venv"})

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).absolute()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._check_link(self.data_dir)
        for name in ("objects", "snapshots"):
            (self.data_dir / name).mkdir(exist_ok=True)
            self._check_link(self.data_dir / name)

    @staticmethod
    def _check_link(path):
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Link or reparse point: {path}")
        return st

    @staticmethod
    def _digest(data):
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Invalid snapshot/object ID")
        return value

    def _root(self, root):
        root = Path(root).absolute()
        if not stat.S_ISDIR(self._check_link(root).st_mode):
            raise ValueError("Workspace must be a directory")
        root = root.resolve(strict=True)
        if root == self.data_dir or root in self.data_dir.resolve().parents:
            raise ValueError("Snapshot storage must be outside the workspace")
        return root

    @staticmethod
    def _relative(name):
        if not isinstance(name, str) or "\\" in name or ":" in name:
            raise ValueError("Invalid snapshot path")
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or name != path.as_posix()
                or any(p in ("", ".", "..") for p in name.split("/"))):
            raise ValueError("Invalid snapshot path")
        return path

    def _path(self, root, name):
        relative = self._relative(name)
        if any(p in self.EXCLUDED for p in relative.parts):
            raise ValueError("Excluded snapshot path")
        path = root.joinpath(*relative.parts)
        for part in (root, *reversed(path.parents)):
            if part != root and root not in part.parents:
                continue
            if part.exists() or part.is_symlink():
                st = self._check_link(part)
                if not stat.S_ISDIR(st.st_mode):
                    raise ConflictError(f"Non-directory ancestor: {part}")
        if path.exists() or path.is_symlink():
            self._check_link(path)
        return path

    @staticmethod
    def _atomic(path, data, mode=None):
        fd, temporary = tempfile.mkstemp(prefix=".rewind-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _save_object(self, data):
        digest = self._digest(data)
        path = self.data_dir / "objects" / digest
        if not path.exists():
            self._atomic(path, data)
        elif self._digest(path.read_bytes()) != digest:
            raise ValueError(f"Corrupt object: {digest}")
        return digest

    def _object(self, digest):
        path = self.data_dir / "objects" / self._id(digest)
        self._check_link(path)
        data = path.read_bytes()
        if self._digest(data) != digest:
            raise ValueError(f"Corrupt object: {digest}")
        return data

    def _read_file(self, path):
        before = self._check_link(path)
        if not stat.S_ISREG(before.st_mode):
            raise ConflictError(f"Not a regular file: {path}")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ConflictError(f"File changed during read: {path}")
            data = stream.read()
            after = os.fstat(stream.fileno())
        final = self._check_link(path)
        if ((opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)):
            raise ConflictError(f"File changed during read: {path}")
        return data, stat.S_IMODE(after.st_mode)

    def capture(self, root):
        """Capture regular files and return a persistent content-addressed ID."""
        root = self._root(root)
        files = {}
        def walk_error(error):
            raise error

        for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            base = Path(directory)
            keep = []
            for name in sorted(dirs):
                if name in self.EXCLUDED:
                    continue
                try:
                    self._check_link(base / name)
                except ValueError:
                    continue
                keep.append(name)
            dirs[:] = keep
            for name in sorted(names):
                path = base / name
                try:
                    self._check_link(path)
                except ValueError:
                    continue
                data, mode = self._read_file(path)
                files[path.relative_to(root).as_posix()] = {
                    "sha256": self._save_object(data), "size": len(data), "mode": mode}
        manifest = {"version": 1, "root": str(root), "files": files}
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        identity = self._digest(payload)
        self._atomic(self.data_dir / "snapshots" / (identity + ".json"), payload)
        return identity

    def _load(self, identity):
        path = self.data_dir / "snapshots" / (self._id(identity) + ".json")
        self._check_link(path)
        payload = path.read_bytes()
        if self._digest(payload) != identity:
            raise ValueError("Corrupt snapshot manifest")
        manifest = json.loads(payload)
        if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("Unsupported snapshot manifest")
        for name, entry in manifest["files"].items():
            self._relative(name)
            self._id(entry["sha256"])
            if not isinstance(entry["mode"], int) or not 0 <= entry["mode"] <= 0o777:
                raise ValueError("Invalid file mode")
        return manifest

    def diff(self, before, after):
        """Return added/deleted/modified records, each with path and metadata."""
        left, right = self._load(before), self._load(after)
        if left["root"] != right["root"]:
            raise ValueError("Snapshots belong to different workspaces")
        a, b = left["files"], right["files"]
        return [{"path": name,
                 "status": "added" if name not in a else "deleted" if name not in b else "modified",
                 "before": a.get(name), "after": b.get(name)}
                for name in sorted(a.keys() | b.keys()) if a.get(name) != b.get(name)]

    def restore(self, root, target, expected, paths=None):
        """Restore changed paths only, after validating ALL current contents.

        A conflicting manual edit aborts before the first workspace write.
        Exceptions during application trigger restoration of already-touched
        files. Redo uses this same method with target and expected exchanged.
        """
        root = self._root(root)
        goal, current = self._load(target), self._load(expected)
        if goal["root"] != str(root) or current["root"] != str(root):
            raise ValueError("Snapshot workspace mismatch")
        changes = self.diff(expected, target)
        if paths is not None:
            selected = set(paths)
            unknown = selected - {c["path"] for c in changes}
            if unknown:
                raise ValueError(f"Paths are not part of this restore: {sorted(unknown)}")
            changes = [c for c in changes if c["path"] in selected]
        prepared = []
        for change in changes:
            name = change["path"]
            path = self._path(root, name)
            entry = current["files"].get(name)
            exists = path.exists()
            if entry is None:
                if exists:
                    raise ConflictError(f"Unexpected existing path: {name}")
            else:
                if not exists:
                    raise ConflictError(f"Expected file is missing: {name}")
                data, mode = self._read_file(path)
                if self._digest(data) != entry["sha256"] or mode != entry["mode"]:
                    raise ConflictError(f"File modified since checkpoint: {name}")
            desired = goal["files"].get(name)
            # Verify all needed objects before touching any workspace file.
            new_data = self._object(desired["sha256"]) if desired else None
            old_data = self._object(entry["sha256"]) if entry else None
            prepared.append((name, desired, new_data, entry, old_data))
        applied, created_dirs = [], []
        try:
            for name, desired, data, old, old_data in prepared:
                path = self._path(root, name)
                # Recheck each file immediately before its mutation as well.
                if old:
                    actual, mode = self._read_file(path)
                    if self._digest(actual) != old["sha256"] or mode != old["mode"]:
                        raise ConflictError(f"Concurrent change: {name}")
                elif path.exists():
                    raise ConflictError(f"Concurrent creation: {name}")
                applied.append((name, old, old_data))
                if desired:
                    missing = []
                    parent = path.parent
                    while parent != root and not parent.exists():
                        missing.append(parent)
                        parent = parent.parent
                    for directory in reversed(missing):
                        directory.mkdir()
                        created_dirs.append(directory)
                    self._path(root, name)
                    self._atomic(path, data, desired["mode"])
                else:
                    path.unlink()
            for name, desired, _, _, _ in prepared:
                path = self._path(root, name)
                if desired:
                    actual, mode = self._read_file(path)
                    if self._digest(actual) != desired["sha256"] or mode != desired["mode"]:
                        raise RuntimeError(f"Restore verification failed: {name}")
                elif path.exists():
                    raise RuntimeError(f"Deletion verification failed: {name}")
        except BaseException as error:
            failures = []
            for name, old, data in reversed(applied):
                try:
                    path = self._path(root, name)
                    if old:
                        self._atomic(path, data, old["mode"])
                    elif path.exists():
                        path.unlink()
                except Exception as rollback_error:
                    failures.append(f"{name}: {rollback_error}")
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if failures:
                raise RuntimeError(f"Restore failed: {error}; rollback incomplete: {failures}") from error
            raise
        return changes
