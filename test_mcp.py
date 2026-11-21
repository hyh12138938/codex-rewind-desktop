import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class TransportTest(unittest.TestCase):
    def test_real_stdio_initialize_discover_and_status(self):
        with tempfile.TemporaryDirectory() as folder:
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'rewind_status', 'arguments': {}}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'rewind_apply', 'arguments': {'token': 'invalid'}}},
            ]
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('desktop.py')), '--mcp', '--data-dir', folder],
                input='\n'.join(json.dumps(r) for r in requests) + '\n',
                capture_output=True, text=True, encoding='utf-8', timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([r['id'] for r in replies], [1, 2, 3, 4])
            self.assertEqual(len(replies[1]['result']['tools']), 7)
            state = json.loads(replies[2]['result']['content'][0]['text'])
            self.assertEqual(state['checkpoints'], {})
            self.assertTrue(replies[3]['result']['isError'])


if __name__ == '__main__':
    unittest.main()
