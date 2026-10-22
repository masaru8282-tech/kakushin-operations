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
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from xlsx_fixture import fixtures,workbook


class ReviewHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));cls.port=sock.getsockname()[1]
        cls.base=f'http://127.0.0.1:{cls.port}'
        cls.process=subprocess.Popen([sys.executable,'-m','kakushin','--state-dir',cls.temp.name,'serve','--review','--host','0.0.0.0','--allowed-host','private-example.app.github.dev','--port',str(cls.port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with urlopen(cls.base+'/api/health',timeout=1) as r:cls.health=json.load(r)
                break
            except OSError:time.sleep(.03)
        else:cls.process.terminate();cls.process.wait(timeout=5);raise RuntimeError('Review server startup failed')
        with urlopen(Request(cls.base,headers={'Host':'private-example.app.github.dev'})) as r:
            cls.html=r.read().decode();cls.headers=r.headers
        cls.csrf=re.search("const csrf='([^']+)'",cls.html).group(1)

    @classmethod
    def tearDownClass(cls):cls.process.terminate();cls.process.wait(timeout=5);cls.temp.cleanup()

    def request(self,path,payload=None,host='private-example.app.github.dev'):
        headers={'Host':host,'Content-Type':'application/json','X-CSRF-Token':self.csrf}
        with urlopen(Request(self.base+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers),timeout=5) as r:return json.load(r)

    def test_review_request_no_persistence_or_external_mutation(self):
        data=Path('examples/reservations.csv').read_bytes();pl,linen=fixtures(data)
        encode=lambda x:base64.b64encode(x).decode()
        payload=dict(csv_base64=encode(data),pl_base64=encode(workbook({'OCT2026':pl})),linen_base64=encode(workbook(linen)),month='2026-10',business_date='2026-10-23',scope='full')
        result=self.request('/api/reconcile',payload)
        self.assertEqual(result['PL']['status'],'MATCH')
        self.assertEqual(result['written_ranges'],[])
        self.assertEqual(self.request('/api/runs'),[])
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])
        self.assertTrue(self.health['review']);self.assertFalse(self.health['demo'])
        self.assertEqual(self.headers['X-Frame-Options'],'DENY')

    def test_unknown_host_and_archive_endpoint_rejected(self):
        for path,payload,host in [('/api/health',None,'untrusted.example'),('/api/run',{},'private-example.app.github.dev')]:
            with self.assertRaises(HTTPError) as e:self.request(path,payload,host)
            self.assertEqual(e.exception.code,403)

    def test_external_review_requires_exact_host(self):
        result=subprocess.run([sys.executable,'-m','kakushin','serve','--review','--host','0.0.0.0'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('--allowed-host',result.stderr)

    def test_daily_schedule_api_preserves_fixed_order_and_does_not_save_inputs(self):
        data=Path('examples/reservations.csv').read_bytes();_,linen=fixtures(data)
        for row in range(4,14):linen['倉庫入出庫履歴']['Y'+str(row)]=0
        encode=lambda x:base64.b64encode(x).decode()
        payload=dict(csv_base64=encode(data),linen_base64=encode(workbook(linen)),business_date='2026-10-23')
        result=self.request('/api/daily-plan',payload)
        self.assertEqual(len(result['report_days']),3)
        self.assertEqual(result['written_ranges'],[])
        self.assertFalse(result['external_sent']);self.assertFalse(result['saved_to_server'])
        self.assertFalse(result['orders'][0]['copy_ready'])
        payload['options']={'order_states':{'2026-10-23':'ORDERED'}}
        result=self.request('/api/daily-plan',payload)
        self.assertTrue(all(i['quantity']==0 for i in result['orders'][0]['items']))
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])
