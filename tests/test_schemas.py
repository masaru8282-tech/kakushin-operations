import csv
import io
import json
from pathlib import Path
import re
import unittest

from kakushin.csvio import parse_csv
from kakushin.engine import aggregate_pl
from kakushin.linen import yatsushiro_supply


class SchemaTests(unittest.TestCase):
    def test_detailed44_status_and_personal_data_not_in_normalized_result(self):
        section=Path('docs/csv-schema.md').read_text()
        headers=[]
        for line in section.splitlines():
            cells=[s.strip() for s in line.split('|')[1:-1]]
            if len(cells)==3 and cells[0].isdigit(): headers.append(cells[1])
        self.assertEqual(len(headers),44)
        good=dict.fromkeys(headers,'')
        good.update({'予約サイト':'Booking.com','物件名':'08_vetta@yatsushiro Egretta','部屋番号':'101',
                     'チェックイン':'2026-10-20','チェックアウト':'2026-10-22','ゲスト数':'1','合計日数':'2',
                     '通貨':'JPY','販売':'10000','OTA サービス料':'1000','AirHost予約ID':'normal',
                     'チャンネル予約ID':'booking1','状態':'確認済み','ゲスト名':'PERSONAL_SENTINEL',
                     'メールアドレス':'PRIVATE_SENTINEL','コメント':'multiple\nlines, ok'})
        cancelled=dict(good);cancelled.update({'AirHost予約ID':'cancelled','チャンネル予約ID':'booking2',
                                               '状態':'システムキャンセル','販売':'0','OTA サービス料':'0'})
        stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=headers);writer.writeheader();writer.writerows([good,cancelled])
        p=parse_csv(stream.getvalue().encode('utf-8-sig'))
        self.assertEqual(p['schema'],'D')
        self.assertFalse(p['issues'])
        self.assertEqual(len(p['reservations']),2)
        result=aggregate_pl(p,'2026-10','full')
        self.assertEqual(result['totals']['sales'],10000)
        self.assertEqual(result['totals']['tax'],0)
        self.assertNotIn('PERSONAL_SENTINEL',json.dumps(p))
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(p))

    def test_yatsushiro_destination_entry_required(self):
        result=yatsushiro_supply('八代:106',{'FACE':5},{'FACE':0},[
            dict(room='八代:103',entry_confirmed=True,stock={'FACE':10},reserved={'FACE':0})])
        self.assertEqual(result['status'],'HELD')
        self.assertEqual(result['transfers'],[])
