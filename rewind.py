"""Desktop rewind coordinator. Conversation originals are retained for redo."""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

from snapshot import SnapshotStore, ConflictError


def atomic_json(path, value):
    SnapshotStore._atomic(Path(path), json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


class Rewind:
    def __init__(self, server, data_dir):
        self.server = server
        self.data = Path(data_dir).absolute()
        self.data.mkdir(parents=True, exist_ok=True)
        self.snapshots = SnapshotStore(self.data / 'files')
        self.mutex = threading.RLock()
        self.state_path = self.data / 'state.json'

    def _load(self):
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
            if state.get('version') != 1:
                raise RuntimeError('Unsupported rewind state version')
            return state
        return {'version': 1, 'checkpoints': {}, 'operations': {}, 'previews': {}}

    @contextmanager
    def locked(self):
        with self.mutex, (self.data / 'writer.lock').open('a+b') as lock:
            if lock.tell() == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield self._load()
            finally:
                lock.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _idle(self, thread_id):
        thread = self.server.read(thread_id)
        if thread.get('historyMode') != 'paginated':
            raise RuntimeError('第一版只支持 paginated 格式的本地 Codex 聊天。')
        turns = self.server.turns(thread_id)
        if thread.get('status', {}).get('type') == 'active' or any(
                t.get('status') == 'inProgress' for t in turns):
            raise RuntimeError('请等该聊天完成当前轮次后操作。')
        return thread, [t['id'] for t in turns]

    @staticmethod
    def _ready(state):
        pending = [o['id'] for o in state['operations'].values()
                   if o['status'] in ('prepared', 'applying', 'recovering')]
        if pending:
            raise RuntimeError(f'发现中断操作，请先 recover：{pending}')

    def status(self):
        with self.locked() as state:
            return state

    def auto_sync(self, thread_id):
        """Capture the latest completed reply once; never relabel later edits as it."""
        with self.locked() as state:
            self._ready(state)
            metadata = self.server.read(thread_id)
            if metadata.get('status', {}).get('type') == 'active':
                return {'tracked': False, 'reason': 'active'}
            if any(t.get('status') == 'inProgress' for t in self.server.turns(thread_id)):
                return {'tracked': False, 'reason': 'active'}
            thread, turns = self._idle(thread_id)
            if not turns:
                return {'tracked': False, 'reason': 'noCompletedTurns'}
            points = state.setdefault('turnCheckpoints', {}).setdefault(thread_id, {})
            if turns[-1] in points:
                return {'tracked': True, 'lastTurn': turns[-1], 'new': False}
            root = str(Path(thread['cwd']).resolve(strict=True))
            snapshot = self.snapshots.capture(root)
            if self._idle(thread_id)[1] != turns:
                raise RuntimeError('聊天在快照期间发生变化，请等当前轮次完成。')
            points[turns[-1]] = {'snapshot': snapshot, 'turns': turns,
                                 'root': root, 'captured': time.time()}
            atomic_json(self.state_path, state)
            return {'tracked': True, 'lastTurn': turns[-1], 'new': True}

    def native_preview(self, thread_id, turn_id, files=True):
        """Keep the clicked reply and rewind everything after it."""
        with self.locked() as state:
            self._ready(state)
            thread, turns = self._idle(thread_id)
            if turn_id not in turns:
                raise RuntimeError('该回复不属于当前可见历史，请重新打开聊天。')
            boundary = turns.index(turn_id) + 1
            if boundary == len(turns):
                raise RuntimeError('这已经是最后一条回复，没有后续轮次需要撤回。')
            prefix = turns[:boundary]
            points = state.get('turnCheckpoints', {}).get(thread_id, {})
            selected = points.get(turn_id)
            latest = points.get(turns[-1])
            available = bool(selected and latest and selected['turns'] == prefix
                             and latest['turns'] == turns)
            if files and not available:
                return {'unavailableFiles': True, 'thread': thread_id, 'turn': turn_id,
                        'reason': '这条回复没有启用插件后保存的文件快照；可选择仅撤回后续聊天。'}
            ident = uuid.uuid4().hex
            cp = {'id': ident, 'label': '回复处撤回', 'thread': thread_id,
                  'root': str(Path(thread['cwd']).resolve(strict=True)),
                  'before': selected['snapshot'] if available else None,
                  'after': latest['snapshot'] if available else None,
                  'baselineTurns': prefix, 'endTurns': turns, 'created': time.time(),
                  'native': True}
            state['checkpoints'][ident] = cp
            changes = self.snapshots.diff(cp['after'], cp['before']) if files else []
            live = self.snapshots._load(self.snapshots.capture(cp['root']))['files'] if files else {}
            conflicts = [c['path'] for c in changes if live.get(c['path']) != c['before']]
            token = secrets.token_urlsafe(24)
            preview = {'token': token, 'mode': 'rewind', 'checkpoint': ident,
                       'operation': None, 'files': files, 'changes': changes,
                       'conflicts': conflicts, 'expires': time.time() + 300,
                       'keepTurn': turn_id, 'removedTurns': len(turns) - boundary}
            state['previews'][token] = preview
            atomic_json(self.state_path, state)
            return preview

    def checkpoint(self, thread_id, label=''):
        with self.locked() as state:
            self._ready(state)
            thread, turns = self._idle(thread_id)
            root = str(Path(thread['cwd']).resolve(strict=True))
            before = self.snapshots.capture(root)
            if self._idle(thread_id)[1] != turns:
                raise RuntimeError('保存期间聊天发生变化，请重试。')
            ident = uuid.uuid4().hex
            cp = {'id': ident, 'label': label or '任务起点', 'thread': thread_id,
                  'root': root, 'before': before, 'baselineTurns': turns,
                  'after': None, 'endTurns': None, 'created': time.time()}
            state['checkpoints'][ident] = cp
            atomic_json(self.state_path, state)
            return cp

    def seal(self, checkpoint):
        with self.locked() as state:
            self._ready(state)
            cp = state['checkpoints'][checkpoint]
            if cp['after'] is not None:
                raise RuntimeError('该任务已记录完成；新任务请保存新的起点。')
            _, turns = self._idle(cp['thread'])
            if turns[:len(cp['baselineTurns'])] != cp['baselineTurns']:
                raise RuntimeError('聊天历史已改变，起点不再匹配。')
            after = self.snapshots.capture(cp['root'])
            if self._idle(cp['thread'])[1] != turns:
                raise RuntimeError('保存期间聊天发生变化，请重试。')
            cp.update(after=after, endTurns=turns)
            atomic_json(self.state_path, state)
            return {'checkpoint': cp, 'changes': self.snapshots.diff(cp['before'], after)}

    def preview(self, checkpoint=None, operation=None, files=True):
        with self.locked() as state:
            self._ready(state)
            if operation:
                op = state['operations'][operation]
                if op['status'] != 'rewound':
                    raise RuntimeError('此回退已撤销，或尚未完成。')
                _, branch_turns = self._idle(op['branch'])
                if branch_turns != op['branchTurns']:
                    raise RuntimeError('回退分支已有新轮次；请先保存其工作，再撤销回退。')
                _, original_turns = self._idle(op['original'])
                if original_turns != op['originalTurns']:
                    raise RuntimeError('原聊天已有新轮次；撤销目标发生变化。')
                target, expected = op['after'], op['before']
                cp = state['checkpoints'][op['checkpoint']]
                mode = 'redo'
                files = op['files']
            else:
                cp = state['checkpoints'][checkpoint]
                if cp['after'] is None:
                    raise RuntimeError('请先在任务完成后点击“记录完成”，再预览回退。')
                _, turns = self._idle(cp['thread'])
                if turns != cp['endTurns']:
                    raise RuntimeError('记录完成后聊天出现新轮次，请为新任务单独保存起点。')
                if len(turns) <= len(cp['baselineTurns']):
                    raise RuntimeError('起点之后没有可回退的轮次。')
                target, expected = cp['before'], cp['after']
                mode = 'rewind'
            changes = self.snapshots.diff(expected, target) if files else []
            live = self.snapshots._load(self.snapshots.capture(cp['root']))['files'] if files else {}
            conflicts = [c['path'] for c in changes if live.get(c['path']) != c['before']]
            token = secrets.token_urlsafe(24)
            preview = {'token': token, 'mode': mode, 'checkpoint': cp['id'],
                       'operation': operation, 'files': files, 'changes': changes,
                       'conflicts': conflicts, 'expires': time.time() + 300}
            state['previews'] = {k: v for k, v in state['previews'].items() if v['expires'] > time.time()}
            state['previews'][token] = preview
            atomic_json(self.state_path, state)
            return preview

    def apply(self, token):
        with self.locked() as state:
            self._ready(state)
            preview = state['previews'].pop(token, None)
            if preview is None or preview['expires'] < time.time():
                raise RuntimeError('预览已过期或已使用，请重新预览。')
            atomic_json(self.state_path, state)
            if preview['conflicts']:
                raise ConflictError(f"存在手动改动冲突：{preview['conflicts']}")
            cp = state['checkpoints'][preview['checkpoint']]
            if preview['mode'] == 'rewind':
                _, turns = self._idle(cp['thread'])
                if turns != cp['endTurns']:
                    raise RuntimeError('聊天自预览后发生变化，请重新预览。')
                # Preserve the original; fork only the prefix. Never truncate a user's log.
                branch = self.server.call('thread/fork', {
                    'threadId': cp['thread'], 'beforeTurnId': turns[len(cp['baselineTurns'])],
                    'excludeTurns': True})['thread']['id']
                branch_turns = self.server.turns(branch)
                if [t['id'] for t in branch_turns] != cp['baselineTurns']:
                    raise RuntimeError(f'分支前缀核验失败；原聊天保持完整。分支：{branch}')
                if self._idle(cp['thread'])[1] != turns:
                    raise RuntimeError('创建分支期间原聊天发生变化，文件保持原样。')
                ident = uuid.uuid4().hex
                op = {'id': ident, 'checkpoint': cp['id'], 'original': cp['thread'],
                      'branch': branch, 'originalTurns': turns, 'branchTurns': cp['baselineTurns'],
                      'before': cp['before'], 'after': cp['after'], 'root': cp['root'],
                      'files': preview['files'], 'status': 'prepared', 'action': 'rewind'}
                state['operations'][ident] = op
                target, expected = cp['before'], cp['after']
            else:
                op = state['operations'][preview['operation']]
                if op['status'] != 'rewound':
                    raise RuntimeError('该回退状态已变化。')
                if self._idle(op['branch'])[1] != op['branchTurns'] or self._idle(op['original'])[1] != op['originalTurns']:
                    raise RuntimeError('聊天自预览后发生变化，请重新预览。')
                op.update(status='prepared', action='redo')
                target, expected = op['after'], op['before']
            op['restoreTarget'], op['restoreExpected'] = target, expected
            atomic_json(self.state_path, state)
            try:
                op['status'] = 'applying'
                atomic_json(self.state_path, state)
                if op['files']:
                    self.snapshots.restore(op['root'], target, expected)
                op['status'] = 'rewound' if op['action'] == 'rewind' else 'redone'
                atomic_json(self.state_path, state)
            except BaseException:
                # The prepared journal remains durable, even when a write or RPC times out.
                # Recovery classifies each affected path before changing anything.
                raise
            active = op['branch'] if op['status'] == 'rewound' else op['original']
            return {'operation': op, 'activeThread': active, 'url': f'codex://threads/{active}'}

    def recover(self, operation):
        """Return interrupted file application to its pre-operation state."""
        with self.locked() as state:
            op = state['operations'][operation]
            if op['status'] not in ('prepared', 'applying', 'recovering'):
                raise RuntimeError('该操作没有中断。')
            if self._idle(op['original'])[1] != op['originalTurns'] or self._idle(op['branch'])[1] != op['branchTurns']:
                raise RuntimeError('聊天已变化；请保留恢复记录并人工检查。')
            if op['files']:
                target, expected = op['restoreTarget'], op['restoreExpected']
                current = self.snapshots.capture(op['root'])
                manifest = self.snapshots._load(current)['files']
                changes = self.snapshots.diff(expected, target)
                if any(manifest.get(c['path']) not in (c['before'], c['after']) for c in changes):
                    raise ConflictError('中断后文件已有额外改动，保留记录供人工恢复。')
                paths = [c['path'] for c in changes if manifest.get(c['path']) == c['after']]
                op['status'] = 'recovering'
                atomic_json(self.state_path, state)
                self.snapshots.restore(op['root'], expected, current, paths=paths)
            op['status'] = 'cancelled' if op['action'] == 'rewind' else 'rewound'
            atomic_json(self.state_path, state)
            return op
