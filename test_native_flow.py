"""Reply-boundary regression checks for the native action-row coordinator."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rewind import Rewind
from snapshot import ConflictError
from test_rewind import FakeServer


class NativeFlowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'workspace'
        self.root.mkdir()
        self.sample = self.root / 'sample.txt'
        self.sample.write_bytes(b'FIRST_REPLY\n')
        self.server = FakeServer(self.root)
        self.data = Path(temporary.name) / 'rewind-data'
        self.core = Rewind(self.server, self.data)

    def complete_later_reply(self, content=b'LATER_REPLY\n', thread='original', turn='later'):
        self.sample.write_bytes(content)
        self.server.threads[thread].append(turn)
        return self.core.auto_sync(thread)

    def test_completed_reply_snapshot_saved_once_even_after_manual_edit_and_restart(self):
        with patch.object(self.core.snapshots, 'capture', wraps=self.core.snapshots.capture) as capture:
            first = self.core.auto_sync('original')
            saved = self.core.status()['turnCheckpoints']['original']['baseline']['snapshot']
            self.sample.write_bytes(b'MANUAL_EDIT\n')
            second = self.core.auto_sync('original')
            self.assertEqual(capture.call_count, 1)
        self.assertTrue(first['new'])
        self.assertFalse(second['new'])
        restarted = Rewind(self.server, self.data)
        self.assertFalse(restarted.auto_sync('original')['new'])
        self.assertEqual(restarted.status()['turnCheckpoints']['original']['baseline']['snapshot'], saved)
        manifest = restarted.snapshots._load(saved)
        content = restarted.snapshots._object(manifest['files']['sample.txt']['sha256'])
        self.assertEqual(content, b'FIRST_REPLY\n')
        self.assertEqual(self.sample.read_bytes(), b'MANUAL_EDIT\n')

    def test_keep_clicked_reply_restore_files_and_redo_after_restart(self):
        self.core.auto_sync('original')
        self.complete_later_reply()
        self.complete_later_reply(b'THIRD_REPLY\n', turn='third')
        preview = self.core.native_preview('original', 'baseline')
        self.assertEqual(preview['keepTurn'], 'baseline')
        self.assertEqual(preview['removedTurns'], 2)
        self.assertEqual(preview['conflicts'], [])
        self.assertEqual([change['path'] for change in preview['changes']], ['sample.txt'])
        result = self.core.apply(preview['token'])
        self.assertEqual(self.sample.read_bytes(), b'FIRST_REPLY\n')
        self.assertEqual(self.server.threads[result['activeThread']], ['baseline'])
        self.assertEqual(self.server.threads['original'], ['baseline', 'later', 'third'])
        restarted = Rewind(self.server, self.data)
        redo_preview = restarted.preview(operation=result['operation']['id'])
        redone = restarted.apply(redo_preview['token'])
        self.assertEqual(redone['activeThread'], 'original')
        self.assertEqual(self.sample.read_bytes(), b'THIRD_REPLY\n')
        self.assertEqual(redone['operation']['status'], 'redone')

    def test_clicked_latest_reply_has_no_later_turns_and_creates_no_preview(self):
        self.core.auto_sync('original')
        with self.assertRaisesRegex(RuntimeError, '最后一条回复'):
            self.core.native_preview('original', 'baseline')
        state = self.core.status()
        self.assertEqual(state['checkpoints'], {})
        self.assertEqual(state['previews'], {})
        self.assertEqual(state['operations'], {})

    def test_missing_historical_snapshot_offers_chat_only_and_redo_preserves_files(self):
        # Plugin was enabled only after both replies already existed.
        self.complete_later_reply()
        unavailable = self.core.native_preview('original', 'baseline')
        self.assertTrue(unavailable['unavailableFiles'])
        self.assertIn('快照', unavailable['reason'])
        self.assertNotIn('token', unavailable)
        self.assertEqual(self.core.status()['checkpoints'], {})
        self.sample.write_bytes(b'INDEPENDENT_MANUAL_CHANGE\n')
        preview = self.core.native_preview('original', 'baseline', files=False)
        self.assertFalse(preview['files'])
        self.assertEqual(preview['changes'], [])
        result = self.core.apply(preview['token'])
        self.assertEqual(self.server.threads[result['activeThread']], ['baseline'])
        self.assertEqual(self.sample.read_bytes(), b'INDEPENDENT_MANUAL_CHANGE\n')
        redo = self.core.apply(self.core.preview(operation=result['operation']['id'])['token'])
        self.assertEqual(redo['activeThread'], 'original')
        self.assertEqual(self.sample.read_bytes(), b'INDEPENDENT_MANUAL_CHANGE\n')

    def test_manual_change_after_completion_is_a_conflict_and_no_file_is_overwritten(self):
        self.core.auto_sync('original')
        self.complete_later_reply()
        self.sample.write_bytes(b'HUMAN\n')
        preview = self.core.native_preview('original', 'baseline')
        self.assertEqual(preview['conflicts'], ['sample.txt'])
        with self.assertRaises(ConflictError):
            self.core.apply(preview['token'])
        self.assertEqual(self.sample.read_bytes(), b'HUMAN\n')
        self.assertNotIn('branch', self.server.threads)

    def test_active_thread_returns_active_without_capturing_or_persisting_a_point(self):
        self.server.active = True
        with patch.object(self.core.snapshots, 'capture', side_effect=AssertionError('active capture')) as capture:
            result = self.core.auto_sync('original')
            capture.assert_not_called()
        self.assertEqual(result, {'tracked': False, 'reason': 'active'})
        self.assertEqual(self.core.status().get('turnCheckpoints', {}), {})
        self.assertEqual(self.sample.read_bytes(), b'FIRST_REPLY\n')

    def test_equal_turn_ids_in_distinct_threads_do_not_share_snapshots(self):
        self.core.auto_sync('original')
        original = self.core.status()['turnCheckpoints']['original']['baseline']['snapshot']
        self.server.threads['other'] = ['baseline']
        self.sample.write_bytes(b'OTHER_THREAD_FIRST\n')
        self.core.auto_sync('other')
        other = self.core.status()['turnCheckpoints']['other']['baseline']['snapshot']
        self.assertNotEqual(original, other)
        self.complete_later_reply(b'OTHER_THREAD_LATER\n', thread='other', turn='other-later')
        preview = self.core.native_preview('other', 'baseline')
        checkpoint = self.core.status()['checkpoints'][preview['checkpoint']]
        self.assertEqual(checkpoint['thread'], 'other')
        self.assertEqual(checkpoint['before'], other)
        self.assertNotEqual(checkpoint['before'], original)
        result = self.core.apply(preview['token'])
        self.assertEqual(self.sample.read_bytes(), b'OTHER_THREAD_FIRST\n')
        self.assertEqual(self.server.threads[result['activeThread']], ['baseline'])
        self.assertEqual(self.server.threads['original'], ['baseline'])

    def test_no_completed_turns_does_not_create_a_fake_snapshot(self):
        self.server.threads['original'] = []
        with patch.object(self.core.snapshots, 'capture', side_effect=AssertionError('empty capture')):
            self.assertEqual(self.core.auto_sync('original'), {'tracked': False, 'reason': 'noCompletedTurns'})
        self.assertEqual(self.core.status().get('turnCheckpoints', {}), {})


if __name__ == '__main__':
    unittest.main()
