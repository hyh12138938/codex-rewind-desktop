"""JSON-RPC bridge to the executable bundled with the installed Codex desktop."""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import subprocess
import threading


def find_codex():
    explicit = os.environ.get('CODEX_REWIND_EXECUTABLE')
    if explicit:
        return str(Path(explicit).resolve(strict=True))
    probe = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                            'Get-AppxPackage -Name OpenAI.Codex | Select-Object -ExpandProperty InstallLocation'],
                           capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    candidates = [Path(line.strip()) / 'app/resources/codex.exe'
                  for line in probe.stdout.splitlines() if line.strip()]
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        raise RuntimeError('未找到 Codex 桌面端；可设置 CODEX_REWIND_EXECUTABLE。')
    def version(p):
        return tuple(int(n) for n in p.parents[2].name.split('_')[1].split('.'))
    return str(max(candidates, key=version))


class AppServer:
    def __init__(self, executable=None, env=None, extra_args=()):
        self.executable = executable or find_codex()
        self.pending = {}
        self.notifications = queue.Queue()
        self.lock = threading.RLock()
        self.next_id = 0
        self.proc = subprocess.Popen(
            [self.executable, 'app-server', '--listen', 'stdio://', *extra_args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', bufsize=1, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.info = self.call('initialize', {
                'clientInfo': {'name': 'codex_rewind_desktop', 'version': '0.1.0'},
                'capabilities': {'experimentalApi': True}})
            self._send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.close()
            raise

    def _send(self, message):
        with self.lock:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if 'id' in msg and ('result' in msg or 'error' in msg):
                    with self.lock:
                        waiter = self.pending.get(msg['id'])
                    if waiter:
                        waiter.put(msg)
                elif 'id' in msg and 'method' in msg:
                    # This bridge never authorizes a command or executes a model tool.
                    self._send({'id': msg['id'], 'error': {
                        'code': -32601, 'message': 'Interactive execution is not supported by rewind bridge'}})
                else:
                    self.notifications.put(msg)
        finally:
            with self.lock:
                for waiter in self.pending.values():
                    waiter.put({'error': {'message': 'Codex app-server exited'}})

    def call(self, method, params=None, timeout=45):
        with self.lock:
            self.next_id += 1
            ident = self.next_id
            waiter = queue.Queue()
            self.pending[ident] = waiter
        try:
            self._send({'id': ident, 'method': method, 'params': params or {}})
            try:
                msg = waiter.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f'Codex 请求超时：{method}；请检查恢复记录后重试') from None
            if 'error' in msg:
                raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
            return msg['result']
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def turns(self, thread_id):
        result, cursor = [], None
        while True:
            page = self.call('thread/turns/list', {
                'threadId': thread_id, 'limit': 100, 'sortDirection': 'asc',
                'itemsView': 'summary', 'cursor': cursor})
            result.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                return result

    def read(self, thread_id):
        return self.call('thread/read', {'threadId': thread_id})['thread']

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream:
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
