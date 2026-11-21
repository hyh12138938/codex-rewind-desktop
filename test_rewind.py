from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rewind import Rewind
from snapshot import ConflictError


class FakeServer:
    def __init__(self, root):
        self.root = str(root)
        self.threads = {'original': ['baseline']}
        self.active = False

    def read(self, ident):
        return {'id': ident, 'cwd': self.root, 'historyMode': 'paginated',
                'status': {'type': 'active' if self.active else 'idle'}}

    def turns(self, ident):
        return [{'id': t, 'status': 'completed'} for t in self.threads[ident]]

    def call(self, method, args):
        assert method == 'thread/fork'
        turns = self.threads[args['threadId']]
        self.threads['branch'] = turns[:turns.index(args['beforeTurnId'])]
        return {'thread': {'id': 'branch'}}


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace'
        self.root.mkdir()
        self.file = self.root / 'sample.txt'
        self.file.write_bytes(b'BASELINE\n')
        self.server = FakeServer(self.root)
        self.data = Path(self.temp.name) / 'data'
        self.core = Rewind(self.server, self.data)
        self.cp = self.core.checkpoint('original', 'integration')
        self.file.write_bytes(b'MODIFIED\n')
        self.server.threads['original'].append('edited')
        self.core.seal(self.cp['id'])

    def test_roundtrip_persisted_and_single_use(self):
        p = self.core.preview(checkpoint=self.cp['id'])
        self.assertEqual([c['path'] for c in p['changes']], ['sample.txt'])
        r = self.core.apply(p['token'])
        self.assertEqual(self.file.read_bytes(), b'BASELINE\n')
        self.assertEqual(self.server.threads['original'], ['baseline', 'edited'])
        self.assertEqual(self.server.threads[r['activeThread']], ['baseline'])
        with self.assertRaises(RuntimeError):
            self.core.apply(p['token'])
        restarted = Rewind(self.server, self.data)
        p = restarted.preview(operation=r['operation']['id'])
        result = restarted.apply(p['token'])
        self.assertEqual(result['activeThread'], 'original')
        self.assertEqual(self.file.read_bytes(), b'MODIFIED\n')

    def test_manual_conflict_and_chat_only(self):
        self.file.write_bytes(b'HUMAN\n')
        p = self.core.preview(checkpoint=self.cp['id'])
        self.assertEqual(p['conflicts'], ['sample.txt'])
        with self.assertRaises(ConflictError):
            self.core.apply(p['token'])
        p = self.core.preview(checkpoint=self.cp['id'], files=False)
        r = self.core.apply(p['token'])
        self.assertEqual(self.file.read_bytes(), b'HUMAN\n')
        self.core.apply(self.core.preview(operation=r['operation']['id'])['token'])
        self.assertEqual(self.file.read_bytes(), b'HUMAN\n')

    def test_preview_to_apply_conflict_is_recoverable(self):
        p = self.core.preview(checkpoint=self.cp['id'])
        self.file.write_bytes(b'HUMAN\n')
        with self.assertRaises(ConflictError):
            self.core.apply(p['token'])
        state = self.core.status()
        op = next(iter(state['operations'].values()))
        self.assertEqual(op['status'], 'applying')
        with self.assertRaises(ConflictError):
            self.core.recover(op['id'])
        self.file.write_bytes(b'MODIFIED\n')
        self.core.recover(op['id'])
        self.assertEqual(self.core.status()['operations'][op['id']]['status'], 'cancelled')

    def test_crash_journal_recovery(self):
        p = self.core.preview(checkpoint=self.cp['id'])
        original_restore = self.core.snapshots.restore
        def interrupted(*args, **kwargs):
            original_restore(*args, **kwargs)
            raise KeyboardInterrupt('simulated interruption after file writes')
        with patch.object(self.core.snapshots, 'restore', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.core.apply(p['token'])
        self.assertEqual(self.file.read_bytes(), b'BASELINE\n')
        restarted = Rewind(self.server, self.data)
        op = next(iter(restarted.status()['operations'].values()))
        restarted.recover(op['id'])
        self.assertEqual(self.file.read_bytes(), b'MODIFIED\n')

    def test_new_turn_and_active_chat_rejected(self):
        p = self.core.preview(checkpoint=self.cp['id'])
        self.server.threads['original'].append('another')
        with self.assertRaises(RuntimeError):
            self.core.apply(p['token'])
        self.assertEqual(self.file.read_bytes(), b'MODIFIED\n')
        self.server.active = True
        with self.assertRaises(RuntimeError):
            self.core.checkpoint('original')


if __name__ == '__main__':
    unittest.main()
