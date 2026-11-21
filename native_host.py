"""Attach the inline reply controls to an already debugging-enabled desktop."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from bridge import AppServer
from desktop import DEFAULT_DATA, dispatch
from rewind import Rewind, atomic_json

ALLOWED = {'auto_sync', 'native_preview', 'preview', 'apply', 'status', 'recover'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise ValueError('Invalid debugging port')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    node = shutil.which('node')
    if not node:
        raise RuntimeError('需要现有 Node.js 22 或更高版本。')
    root = Path(__file__).resolve().parent
    args.data_dir.mkdir(parents=True, exist_ok=True)
    log = args.data_dir / 'native-host.jsonl'
    stop_file = args.data_dir / 'native.stop'
    stop_file.unlink(missing_ok=True)
    write_lock = threading.Lock()
    with AppServer() as app, ThreadPoolExecutor(max_workers=2) as pool:
        core = Rewind(app, args.data_dir)
        child = subprocess.Popen([node, str(root / 'native_cdp.mjs'), str(args.port)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', bufsize=1,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def record(value):
            value['at'] = time.time()
            with write_lock:
                with log.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(value, ensure_ascii=False) + '\n')

        def respond(request):
            reply = {'type': 'response', 'target': request['target'],
                     'context': request['context'], 'id': request['id']}
            try:
                method = request.get('method')
                payload = request.get('args', {})
                if method not in ALLOWED or not isinstance(payload, dict):
                    raise ValueError('Unsupported native operation')
                if method == 'auto_sync':
                    result = core.auto_sync(payload['thread_id'])
                elif method == 'native_preview':
                    result = core.native_preview(payload['thread_id'], payload['turn_id'], payload.get('files', True))
                elif method == 'apply':
                    # A fork is loaded by its creating app-server. Disconnect
                    # that server before the desktop resumes the new branch.
                    with AppServer() as transaction_app:
                        result = Rewind(transaction_app, args.data_dir).apply(payload['token'])
                else:
                    result = dispatch(core, method, payload)
                reply['result'] = result
                # No transcript or file data in runtime transport logs.
                record({'type': 'operation', 'method': method, 'ok': True})
            except Exception as error:
                reply['error'] = str(error)
                record({'type': 'operation', 'method': request.get('method'), 'ok': False, 'message': str(error)})
            with write_lock:
                if child.poll() is None and not child.stdin.closed:
                    child.stdin.write(json.dumps(reply, ensure_ascii=False) + '\n')
                    child.stdin.flush()

        def errors():
            for line in child.stderr:
                record({'type': 'node-stderr', 'message': line.rstrip()})
        threading.Thread(target=errors, daemon=True).start()
        def watch_stop():
            while child.poll() is None:
                if stop_file.exists():
                    with write_lock:
                        child.stdin.close()
                    return
                time.sleep(0.5)
        threading.Thread(target=watch_stop, daemon=True).start()
        try:
            for line in child.stdout:
                value = json.loads(line)
                if value.get('type') == 'request':
                    pool.submit(respond, value)
                else:
                    record(value)
                    if value.get('type') == 'injected':
                        print('NATIVE REPLY ICON INJECTED', flush=True)
                        atomic_json(args.data_dir / 'native-connection.json', {'port': args.port, 'target': value['target'], 'pid': os.getpid()})
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)


if __name__ == '__main__':
    main()
