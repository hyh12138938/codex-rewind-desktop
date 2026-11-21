"""Local desktop panel and MCP entry point. Python 3.10+, no dependencies."""
from __future__ import annotations
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import webbrowser

from bridge import AppServer
from rewind import Rewind

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CodexRewindDesktop'


def dispatch(core, method, args):
    if method == 'status':
        return core.status()
    if method == 'threads':
        page = core.server.call('thread/list', {'limit': 100, 'modelProviders': [], 'sortKey': 'updated_at'})
        return [{'id': t['id'], 'name': t.get('name') or t.get('preview') or t['id'],
                 'cwd': t['cwd'], 'historyMode': t.get('historyMode'),
                 'status': t.get('status')} for t in page['data']]
    if method == 'checkpoint':
        return core.checkpoint(args['thread_id'], args.get('label', ''))
    if method == 'seal':
        return core.seal(args['checkpoint'])
    if method == 'preview':
        return core.preview(checkpoint=args.get('checkpoint'), operation=args.get('operation'), files=args.get('files', True))
    if method == 'apply':
        return core.apply(args['token'])
    if method == 'recover':
        return core.recover(args['operation'])
    raise ValueError('Unknown operation')


TOOLS = [
    ('rewind_status', '列出已保存起点与回退/redo状态。', {}),
    ('rewind_threads', '列出最近100个本地 Codex 聊天，用真实ID选择。', {}),
    ('rewind_checkpoint', '在空闲聊天开始新任务之前，保存文件与聊天起点。',
     {'thread_id': {'type': 'string'}, 'label': {'type': 'string'}}),
    ('rewind_seal', '在该聊天任务完成并空闲后，记录结束快照。', {'checkpoint': {'type': 'string'}}),
    ('rewind_preview', '预览回退，或通过operation预览撤销回退。返回需确认的一次性token。',
     {'checkpoint': {'type': 'string'}, 'operation': {'type': 'string'}, 'files': {'type': 'boolean'}}),
    ('rewind_apply', '用户确认预览清单后执行回退/redo，返回需打开的Codex聊天URL。', {'token': {'type': 'string'}}),
    ('rewind_recover', '将中断操作的文件恢复到操作前；额外改动会阻止覆盖。', {'operation': {'type': 'string'}}),
]


def mcp(core):
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if 'id' not in request:
                continue
            ident, method = request['id'], request.get('method')
            if method == 'initialize':
                result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'codex-rewind-desktop', 'version': '0.1.0'}}
            elif method == 'tools/list':
                result = {'tools': [{'name': name, 'description': description,
                          'inputSchema': {'type': 'object', 'properties': properties,
                                          'additionalProperties': False}}
                         for name, description, properties in TOOLS]}
            elif method == 'tools/call':
                params = request['params']
                name = params.get('name', '')
                if name not in {t[0] for t in TOOLS}:
                    raise ValueError('Unknown tool')
                try:
                    value = dispatch(core, name.removeprefix('rewind_'), params.get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}]}
                except Exception as error:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': str(error)}]}
            elif method == 'ping':
                result = {}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': ident,
                                  'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': result}, ensure_ascii=False), flush=True)
        except Exception as error:
            print(json.dumps({'jsonrpc': '2.0', 'id': request.get('id') if isinstance(locals().get('request'), dict) else None,
                              'error': {'code': -32602, 'message': str(error)}}), flush=True)


def panel(core, port, no_browser):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, content_type='application/json; charset=utf-8'):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get('Host') != server.expected_host:
                return self.reply(403, {'error': 'Invalid host'})
            if self.path.split('?')[0] == '/':
                return self.reply(200, (HERE / 'panel.html').read_bytes(), 'text/html; charset=utf-8')
            return self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            origin = self.headers.get('Origin')
            if (self.headers.get('Host') != server.expected_host
                    or (origin and origin != server.origin)
                    or not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)):
                return self.reply(403, {'error': 'Invalid local access token'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32768:
                    raise ValueError('Invalid request size')
                body = json.loads(self.rfile.read(size))
                result = dispatch(core, body['method'], body.get('args', {}))
                self.reply(200, {'result': result})
            except Exception as error:
                self.reply(409, {'error': str(error)})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.expected_host = '127.0.0.1:' + str(server.server_port)
    server.origin = 'http://' + server.expected_host
    url = server.origin + '/#' + token
    print('Codex Rewind Desktop 0.1.0', flush=True)
    print(url, flush=True)
    print('本地数据：' + str(core.data), flush=True)
    print('关闭此进程停止面板；恢复记录保留。', flush=True)
    if not no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--mcp', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stdin.reconfigure(encoding='utf-8')
    with AppServer() as server:
        core = Rewind(server, args.data_dir)
        if args.mcp:
            mcp(core)
        else:
            panel(core, args.port, args.no_browser)


if __name__ == '__main__':
    main()
