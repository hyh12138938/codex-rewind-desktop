"""Explicit live test: creates disposable local Codex chats and uses model quota."""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import time
from bridge import AppServer
from rewind import Rewind, atomic_json


def turn(app, thread, prompt):
    app.call('thread/resume', {'threadId': thread, 'excludeTurns': True})
    ident = app.call('turn/start', {'threadId': thread, 'input': [{'type': 'text', 'text': prompt}]})['turn']['id']
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            event = app.notifications.get(timeout=5)
        except queue.Empty:
            continue
        if event.get('method') == 'turn/completed' and event['params']['threadId'] == thread and event['params']['turn']['id'] == ident:
            result = event['params']['turn']
            if result['status'] != 'completed':
                raise RuntimeError(result)
            return '\n'.join(i['text'] for i in result['items'] if i['type'] == 'agentMessage')
    raise TimeoutError('Live test turn did not complete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--seed-record', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / 'NATIVE_VERIFICATION.json'
    if report_path.exists() and not args.resume:
        raise RuntimeError('Use a fresh run directory; this test never overwrites a previous report.')
    report = json.loads(report_path.read_text(encoding='utf-8')) if args.resume else {'status': 'running', 'steps': []}
    def record(step, **values):
        report['steps'].append({'step': step, **values})
        atomic_json(report_path, report)
        print(json.dumps(report['steps'][-1], ensure_ascii=False), flush=True)
    with AppServer() as app:
        report['executable'] = app.executable
        report['runtime'] = app.info
        if args.resume:
            core = Rewind(app, root / 'data')
            state = core.status()
            cp = next(iter(state['checkpoints'].values()))
            op = next(iter(state['operations'].values()))
            thread, workspace = cp['thread'], Path(cp['root'])
            baseline, original = cp['baselineTurns'], cp['endTurns']
            branch = op['branch']
            result = {'operation': op, 'activeThread': branch}
        elif args.seed_record:
            seed = json.loads(args.seed_record.read_text(encoding='utf-8'))
            thread, workspace = seed['thread'], Path(seed['root'])
            assert len(app.turns(thread)) == 1, 'Seed must be our one-turn disposable test chat'
        else:
            workspace = root / 'workspace'
            workspace.mkdir()
            thread = app.call('thread/start', {'cwd': str(workspace), 'historyMode': 'paginated',
                'baseInstructions': 'You are a disposable integration-test assistant. Follow the requested text assertion. Do not use tools.',
                'developerInstructions': 'Reply only with the requested short token or answer.'})['thread']['id']
            app.call('thread/name/set', {'threadId': thread, 'name': 'Rewind Desktop · 集成测试'})
            assert turn(app, thread, 'Reply exactly REWIND_BASELINE_OK') == 'REWIND_BASELINE_OK'
        sample = workspace / 'sample.txt'
        if not args.resume:
            sample.write_bytes(b'BASELINE\n')
            (workspace / 'deleted.bin').write_bytes(bytes(range(256)))
            baseline = [t['id'] for t in app.turns(thread)]
            record('BASELINE', thread=thread, turns=len(baseline), file=sample.read_text().strip())
            core = Rewind(app, root / 'data')
            cp = core.checkpoint(thread, '原生桌面端双向回退测试')
            assert turn(app, thread, 'Remember the marker REMOVED_MARKER_K17. Reply exactly MODIFIED_OK') == 'MODIFIED_OK'
            sample.write_bytes(b'MODIFIED\n')
            (workspace / 'deleted.bin').unlink()
            (workspace / 'created.bin').write_bytes(b'\x00\xff\x01')
            core.seal(cp['id'])
            original = [t['id'] for t in app.turns(thread)]
            record('MODIFIED', thread=thread, turns=len(original), file=sample.read_text().strip())
            preview = core.preview(checkpoint=cp['id'])
            assert not preview['conflicts'], preview
            result = core.apply(preview['token'])
            branch = result['activeThread']
        assert [t['id'] for t in app.turns(branch)] == baseline
        assert [t['id'] for t in app.turns(thread)] == original
        assert sample.read_bytes() == b'BASELINE\n'
        assert (workspace / 'deleted.bin').read_bytes() == bytes(range(256))
        assert not (workspace / 'created.bin').exists()
        record('REWIND', thread=branch, originalThread=thread, turns=len(app.turns(branch)),
               file=sample.read_text().strip(), operation=result['operation']['id'])
        # Verify the model actually sees the shorter history, using a throwaway probe fork.
        probe = app.call('thread/fork', {'threadId': branch, 'excludeTurns': True})['thread']['id']
        question = '列出此前用户消息中出现的所有标记或全大写测试字符串。不要把当前消息算进去。仅输出那些字符串，每行一个。'
        answer = turn(app, probe, question)
        assert 'REWIND_BASELINE_OK' in answer and 'REMOVED_MARKER_K17' not in answer, answer
        record('REWOUND_CONTEXT', answer=answer, probeThread=probe)
        # Restart both coordinator and native app-server before redo.
    with AppServer() as restarted:
        core = Rewind(restarted, root / 'data')
        preview = core.preview(operation=result['operation']['id'])
        redone = core.apply(preview['token'])
        assert redone['activeThread'] == thread
        assert [t['id'] for t in restarted.turns(thread)] == original
        assert sample.read_bytes() == b'MODIFIED\n'
        assert not (workspace / 'deleted.bin').exists()
        assert (workspace / 'created.bin').read_bytes() == b'\x00\xff\x01'
        record('REDO_AFTER_RESTART', thread=thread, turns=len(original), file=sample.read_text().strip())
        probe = restarted.call('thread/fork', {'threadId': thread, 'excludeTurns': True})['thread']['id']
        answer = turn(restarted, probe, question)
        assert 'REMOVED_MARKER_K17' in answer, answer
        record('RESTORED_CONTEXT', answer=answer, probeThread=probe)
        report['status'] = 'PASS'
        report['workspace'] = str(workspace)
        report['checkpoint'] = cp['id']
        report['originalThread'] = thread
        report['rewoundThread'] = branch
        report['sampleSha256'] = hashlib.sha256(sample.read_bytes()).hexdigest()
        atomic_json(report_path, report)
        print('NATIVE ROUNDTRIP PASS', flush=True)


if __name__ == '__main__':
    main()
