"""Repeatable file-copy baseline, modification, and rollback proof."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
from snapshot import SnapshotStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['baseline', 'modified', 'rollback'])
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    base = args.directory.resolve()
    if args.action == 'baseline':
        base.mkdir(parents=True, exist_ok=True)
        if (base / 'fixture.json').exists():
            raise RuntimeError('Use a fresh fixture directory')
        root = base / 'workspace'
        root.mkdir()
        original = b'chat_cursor=2\nfile_state=BASELINE\n'
        (base / 'ORIGINAL_FILE').write_bytes(original)
        (root / 'MODIFIED_FILE').write_bytes(original)
        store = SnapshotStore(base / 'store')
        before = store.capture(root)
        (base / 'fixture.json').write_text(json.dumps({'before': before,
            'original_sha256': hashlib.sha256(original).hexdigest()}), encoding='utf-8')
        print('BASELINE: chat_cursor=2 file_state=BASELINE original_sha256=' + hashlib.sha256(original).hexdigest())
    elif args.action == 'modified':
        root = base / 'workspace'
        original = (base / 'ORIGINAL_FILE').read_bytes()
        modified = b'chat_cursor=1\nfile_state=REWOUND\n'
        (root / 'MODIFIED_FILE').write_bytes(modified)
        store = SnapshotStore(base / 'store')
        after = store.capture(root)
        meta = json.loads((base / 'fixture.json').read_text(encoding='utf-8'))
        meta['after'] = after
        (base / 'fixture.json').write_text(json.dumps(meta), encoding='utf-8')
        (base / 'DIFF_FILE').write_text(''.join(difflib.unified_diff(
            original.decode().splitlines(True), modified.decode().splitlines(True),
            fromfile='ORIGINAL_FILE', tofile='MODIFIED_FILE')), encoding='utf-8')
        # Rollback is exercised on another workspace copy with its own root-bound snapshots.
        copy = base / 'rollback-copy'
        copy.mkdir()
        (copy / 'MODIFIED_FILE').write_bytes(original)
        rollback_before = store.capture(copy)
        (copy / 'MODIFIED_FILE').write_bytes(modified)
        rollback_after = store.capture(copy)
        meta.update(rollback_before=rollback_before, rollback_after=rollback_after)
        (base / 'fixture.json').write_text(json.dumps(meta), encoding='utf-8')
        print('MODIFIED: chat_cursor=1 file_state=REWOUND original_hash_preserved=true')
    else:
        meta = json.loads((base / 'fixture.json').read_text(encoding='utf-8'))
        store = SnapshotStore(base / 'store')
        store.restore(base / 'rollback-copy', meta['rollback_before'], meta['rollback_after'])
        restored = (base / 'rollback-copy/MODIFIED_FILE').read_bytes()
        assert hashlib.sha256(restored).hexdigest() == meta['original_sha256']
        assert (base / 'workspace/MODIFIED_FILE').read_bytes() == b'chat_cursor=1\nfile_state=REWOUND\n'
        assert hashlib.sha256((base / 'ORIGINAL_FILE').read_bytes()).hexdigest() == meta['original_sha256']
        print('ROLLBACK: chat_cursor=2 file_state=BASELINE byte_identical=true modified_copy_retained=true')


if __name__ == '__main__':
    main()
