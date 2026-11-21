import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request


class PanelTest(unittest.TestCase):
    def test_loopback_panel_token_and_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('desktop.py')),
                '--no-browser', '--data-dir', folder], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8')
            lines = queue.Queue()
            def reader():
                for line in process.stdout:
                    lines.put(line.strip())
            threading.Thread(target=reader, daemon=True).start()
            try:
                self.assertIn('Codex Rewind Desktop', lines.get(timeout=25))
                url = lines.get(timeout=5)
                address, token = url.split('#')
                page = urllib.request.urlopen(address, timeout=10).read().decode('utf-8')
                self.assertIn('回退与恢复', page)
                payload = json.dumps({'method': 'status'}).encode()
                def request(auth=None, origin=None):
                    headers = {'Content-Type': 'application/json'}
                    if auth:
                        headers['Authorization'] = 'Bearer ' + auth
                    if origin:
                        headers['Origin'] = origin
                    return urllib.request.urlopen(urllib.request.Request(address, data=payload, headers=headers), timeout=10)
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    request()
                self.assertEqual(missing.exception.code, 403)
                with self.assertRaises(urllib.error.HTTPError) as cross_origin:
                    request(token, 'https://example.com')
                self.assertEqual(cross_origin.exception.code, 403)
                response = json.load(request(token, address.rstrip('/')))
                self.assertEqual(response['result']['checkpoints'], {})
            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
