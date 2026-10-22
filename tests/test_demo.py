import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class DemoHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));cls.port=sock.getsockname()[1]
        cls.base=f'http://127.0.0.1:{cls.port}'
        cls.process=subprocess.Popen([sys.executable,'-m','kakushin','serve','--demo','--host','0.0.0.0','--port',str(cls.port)],
                                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with urlopen(cls.base+'/api/health',timeout=1) as r:
                    cls.health=json.load(r)
                break
            except OSError:time.sleep(.03)
        else:
            cls.process.terminate();cls.process.wait(timeout=5)
            raise RuntimeError('Demo did not start')
        with urlopen(Request(cls.base,headers={'Host':'sample-8000.app.github.dev'})) as r:
            cls.html=r.read().decode();cls.headers=r.headers
        cls.csrf=re.search("const csrf='([^']+)'",cls.html).group(1)

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate();cls.process.wait(timeout=5)

    def post(self,path,payload,csrf=None):
        headers={'Host':'sample-8000.app.github.dev','Content-Type':'application/json','X-CSRF-Token':self.csrf if csrf is None else csrf}
        with urlopen(Request(self.base+path,data=json.dumps(payload).encode(),headers=headers),timeout=5) as r:
            return json.load(r)

    def test_proxy_host_and_frame_headers(self):
        self.assertTrue(self.health['demo'])
        self.assertNotIn('X-Frame-Options',self.headers)
        self.assertIn('frame-ancestors https:',self.headers['Content-Security-Policy'])
        self.assertIn('const demo=true;',self.html)

    def test_sample_button_and_replay(self):
        first=self.post('/api/demo',{})
        second=self.post('/api/demo',{})
        self.assertEqual(first['result']['PL']['totals']['sales'],35000)
        self.assertEqual(first['result']['PL']['totals']['tax'],3800)
        self.assertTrue(second['reused'])
        self.assertEqual(first['result']['run_id'],second['result']['run_id'])
        self.assertEqual(first['result']['written_ranges'],[])

    def test_real_uploads_and_missing_csrf_rejected(self):
        for path,kwargs in [('/api/run',{}),('/api/demo',{'csrf':'invalid'})]:
            with self.assertRaises(HTTPError) as e:self.post(path,{},**kwargs)
            self.assertEqual(e.exception.code,403)

    def test_external_binding_without_demo_rejected(self):
        result=subprocess.run([sys.executable,'-m','kakushin','serve','--host','0.0.0.0'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('--demo',result.stderr)

    def test_private_exception_configuration_is_not_read(self):
        from pathlib import Path
        from unittest.mock import patch
        from kakushin.store import Store
        from kakushin.service import run_csv
        with tempfile.TemporaryDirectory() as root:
            with patch('kakushin.csvio.load_exceptions', side_effect=AssertionError('private configuration read')):
                result, _ = run_csv(Store(root), Path('kakushin/demo.csv').read_bytes(),
                    '2026-10','full','2026-10-22',private_exceptions=False)
                self.assertEqual(result['PL']['totals']['sales'],35000)
