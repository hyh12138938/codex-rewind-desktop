"""Install/remove the local plugin through Codex's native plugin manager."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from bridge import AppServer, find_codex

ROOT = Path(__file__).resolve().parent
NAME = 'codex-rewind-desktop@codex-rewind-local'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, help='Isolated Codex home for installation tests')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    env = os.environ.copy()
    if args.home:
        env['CODEX_HOME'] = str(args.home.resolve())
        args.home.mkdir(parents=True, exist_ok=True)
    executable = find_codex()
    if args.status:
        with AppServer(executable=executable, env=env) as app:
            info = app.call('plugin/read', {'pluginName': 'codex-rewind-desktop',
                'marketplacePath': str(ROOT / '.agents/plugins/marketplace.json')})['plugin']
            print('installed=' + str(info['summary']['installed']).lower())
            print('enabled=' + str(info['summary']['enabled']).lower())
            print('mcp=' + ','.join(info['mcpServers']))
        return
    if args.uninstall:
        commands = [['plugin', 'remove', NAME], ['plugin', 'marketplace', 'remove', 'codex-rewind-local']]
    else:
        # Codex copies the plugin to its cache. The launcher deliberately uses this
        # reviewed source directory; rerun the installer after moving this folder.
        config = {'mcpServers': {'codex_rewind_desktop': {
            'command': sys.executable, 'args': [str(ROOT / 'desktop.py'), '--mcp']}}}
        (ROOT / '.mcp.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
        commands = [['plugin', 'marketplace', 'add', str(ROOT)], ['plugin', 'add', NAME]]
    for command in commands:
        result = subprocess.run([executable, *command], env=env, capture_output=True,
                                text=True, encoding='utf-8', timeout=60,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.stdout:
            print(result.stdout.strip())
        if result.returncode:
            print(result.stderr.strip(), file=sys.stderr)
            raise SystemExit(result.returncode)
    print('PLUGIN REMOVED; SNAPSHOTS RETAINED' if args.uninstall else 'PLUGIN INSTALLED; RELOAD CODEX TO LOAD TOOLS')


if __name__ == '__main__':
    main()
