import base64
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class LocalHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            cls.port=sock.getsockname()[1]
        cls.base=f'http://127.0.0.1:{cls.port}'
        cls.process=subprocess.Popen([sys.executable,'-m','kakushin','--state-dir',cls.temp.name,'serve','--port',str(cls.port)],
                                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if cls.process.poll() is not None: raise RuntimeError('Server failed to start')
                try:
                    with urlopen(cls.base+'/api/health',timeout=1) as r:
                        if r.status==200: break
                except OSError: time.sleep(.03)
            else: raise RuntimeError('Server readiness timeout')
            with urlopen(cls.base) as r:
                cls.html=r.read().decode()
                cls.csp=r.headers['Content-Security-Policy']
            cls.csrf=re.search("const csrf='([^']+)'",cls.html).group(1)
        except Exception:
            cls.process.terminate();cls.process.wait(timeout=5);cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.temp.cleanup()

    def post(self,path,data,token=True,host=None):
        headers={'Content-Type':'application/json'}
        if token:headers['X-CSRF-Token']=self.csrf
        if host:headers['Host']=host
        with urlopen(Request(self.base+path,data=json.dumps(data).encode(),headers=headers),timeout=5) as r:
            return json.load(r)

    def test_csv_to_history_and_tools(self):
        data=Path('examples/reservations.csv').read_bytes()
        payload=dict(csv_base64=base64.b64encode(data).decode(),month='2026-10',business_date='2026-10-22',completeness='full')
        a=self.post('/api/run',payload)
        self.assertEqual(a['result']['PL']['totals']['sales'],35000)
        self.assertEqual(a['result']['linen']['events'][0]['quantities']['FACE'],9)
        b=self.post('/api/run',payload)
        self.assertTrue(b['reused'])
        self.assertEqual(a['result']['run_id'],b['result']['run_id'])
        with urlopen(self.base+'/api/runs/'+a['result']['run_id']) as response:
            self.assertEqual(json.load(response),a['result'])
        with urlopen(self.base+'/api/runs') as response:
            self.assertEqual(len(json.load(response)),1)
        order=self.post('/api/tools',dict(operation='supplier_order',args=dict(item='PILLOW',opening=6,dispatch_today=20,dispatch_until_next=10)))
        self.assertEqual(order['quantity'],50)

    def test_csrf_host_and_input_validation(self):
        for kwargs in ({'token':False},{'host':'evil.example'}):
            with self.assertRaises(HTTPError) as error:
                self.post('/api/tools',{},**kwargs)
            self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError) as error:
            self.post('/api/run',{'csv_base64':'invalid'})
        self.assertEqual(error.exception.code,400)
        self.assertIn("frame-ancestors 'none'",self.csp)
        self.assertIn('シャドーモード',self.html)
        self.assertNotIn('__CSRF__',self.html)
