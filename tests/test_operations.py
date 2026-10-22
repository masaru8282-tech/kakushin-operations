import csv
import hashlib
import hmac
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kakushin.adapters import verify_slack_signature
from kakushin.csvio import parse_csv
from kakushin.engine import aggregate_pl, preparation, additional_preparation, build_preparations
from kakushin.linen import (parse_stock_sequence, reconcile, supplier_order, simulate_stock,
                           entry_window, target_delivery, yatsushiro_supply, transfer_balances, next_receipt)
from kakushin.master import MASTER, RULE_VERSION
from kakushin.projection import preview_pl, ProjectionError, verify_readback
from kakushin.service import run_csv
from kakushin.store import Store

HEADERS = ['予約サイト', 'チェックイン', 'チェックアウト', '物件名', '部屋番号', 'ゲスト数',
           '合計日数', '通貨', '販売', 'OTA サービス料', '予約日', '物件タグ', '宿泊税']


def row(**changes):
    r = dict(zip(HEADERS, ['Airbnb', '2026/9/30', '2026/10/2', '01_ intro vetta', '1001',
                           '3', '2', 'JPY', '10000', '1000', '2026/9/1', '', '0']))
    r.update(changes)
    return r


def csv_bytes(rows, ids=False, cancel=False):
    out = io.StringIO(newline='')
    headers = HEADERS + (['チャンネル予約ID', 'AirHost予約ID'] if ids else ['キャンセル'] if cancel else [])
    writer = csv.DictWriter(out, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode('utf-8-sig')


def parsed(rows, **kwargs):
    fake = {
        'TEST-CANCEL-A':dict(room='桜町:702',ota='Airbnb',sales=1200,fee=120,
            ci='2026-10-20',co='2026-10-24',column='L',month='2026-10'),
        'TEST-CANCEL-B':dict(room=None,ota='Booking.com',sales=2400,fee=240,
            ci='2026-10-12',co='2026-10-14',column='AJ',month='2026-10',guests=1),
    }
    with patch('kakushin.csvio.load_exceptions', return_value=fake):
        return parse_csv(csv_bytes(rows, **kwargs))


class CSVAndPLTests(unittest.TestCase):
    def test_t01_co_month_full_amount_and_tax(self):
        p = parsed([row()])
        r = aggregate_pl(p, '2026-10', 'full')
        self.assertEqual(r['status'], 'SHADOW_VALIDATED')
        self.assertEqual(r['totals'], dict(sales=10000, count=1, nights=2, booking_fee=0, airbnb_fee=1000, tax=1200))
        self.assertEqual(len(r['candidate_cells']), 210)
        self.assertIsNone(r['candidate_cells']['AI27'])
        self.assertEqual(r['candidate_cells']['AF6'], 0)

    def test_t02_different_month_cannot_zero_replace(self):
        r = aggregate_pl(parsed([row(**{'チェックイン':'2026-10-31','チェックアウト':'2026-11-2'})]), '2026-10', 'full')
        self.assertEqual(r['status'], 'HELD')
        self.assertEqual(r['totals']['sales'], 0)

    def test_t32_no_silent_idless_dedup(self):
        p = parsed([row(), row()])
        self.assertEqual(len(p['reservations']), 2)
        self.assertEqual(aggregate_pl(p, '2026-10', 'full')['status'], 'HELD')

    def test_same_id_is_error(self):
        r = row(**{'AirHost予約ID':'123','チャンネル予約ID':'a'})
        p = parsed([r, r], ids=True)
        self.assertTrue(any(x['code'] == 'INPUT_INVALID' for x in p['issues']))

    def test_t33_partial_cannot_replace(self):
        self.assertEqual(aggregate_pl(parsed([row()]), '2026-10', 'partial')['status'], 'HELD')

    def test_t34_scoped_cancellation(self):
        r = row(**{'物件名':'03_vetta@kumamoto sakuramachi','部屋番号':'702',
                   'チェックイン':'','チェックアウト':'','合計日数':'0',
                   '販売':'1200','OTA サービス料':'120','AirHost予約ID':'TEST-CANCEL-A','チャンネル予約ID':'z'})
        p = parsed([r], ids=True)
        result = aggregate_pl(p, '2026-10', 'full')
        self.assertEqual(result['status'], 'SHADOW_VALIDATED')
        self.assertEqual(result['columns']['L'], dict(sales=1200,count=1,nights=0,booking_fee=0,airbnb_fee=120,tax=0))
        self.assertEqual(build_preparations(p, '2026-10-22')['events'], [])
        self.assertEqual(aggregate_pl(p, '2026-11', 'full')['totals']['sales'], 0)
        r['販売'] = '1201'
        self.assertTrue(parsed([r], ids=True)['issues'])

    def test_t35_yatsushiro_cancellation_blank_room(self):
        r = row(**{'予約サイト':'Booking.com','物件名':'08_vetta@yatsushiro Egretta','部屋番号':'',
                   'チェックイン':'','チェックアウト':'','合計日数':'0','ゲスト数':'1',
                   '販売':'2400','OTA サービス料':'240','AirHost予約ID':'TEST-CANCEL-B','チャンネル予約ID':'x'})
        result = aggregate_pl(parsed([r], ids=True), '2026-10', 'full')
        self.assertEqual(result['status'], 'SHADOW_VALIDATED')
        self.assertEqual(result['columns']['AJ'], dict(sales=2400,count=1,nights=0,booking_fee=240,airbnb_fee=0,tax=None))

    def test_t36_unknown_cancel_fee_held(self):
        p = parsed([row(**{'キャンセル':'2026-10-1'})], cancel=True)
        self.assertEqual(aggregate_pl(p, '2026-10', 'full')['status'], 'HELD')
        self.assertEqual(build_preparations(p, '2026-10-02')['events'], [])

    def test_unknown_ota_currency_mapping_and_dates(self):
        for changes in ({'予約サイト':'手動作成'}, {'通貨':'USD'}, {'販売':'-1'}, {'ゲスト数':''},
                        {'部屋番号':'9999'}, {'チェックアウト':'2026-10-3'}, {'チェックイン':'46296'}):
            with self.subTest(changes=changes):
                self.assertEqual(aggregate_pl(parsed([row(**changes)]), '2026-10', 'full')['status'], 'HELD')

    def test_multiline_csv_and_encoding(self):
        p = parsed([row(**{'物件タグ':'line1,\nline2'})])
        self.assertEqual(len(p['reservations']), 1)
        self.assertFalse(p['issues'])
        self.assertTrue(parse_csv(b'\xff\xfe')['issues'])

    def test_pl510_not_approved_but_linen_is(self):
        p = parsed([row(**{'物件名':'07_vetta@kumamoto suizenji2','部屋番号':'510'})])
        self.assertFalse(p['issues'])
        self.assertEqual(aggregate_pl(p, '2026-10', 'full')['status'], 'HELD')
        self.assertEqual(p['reservations'][0]['room'], '水前寺②:510')


class LinenTests(unittest.TestCase):
    def test_master_counts(self):
        self.assertEqual(len(MASTER['rooms']), 39)
        self.assertEqual(len(MASTER['pl_headers']), 35)

    def test_t03_t04_towels_and_fixed_bedding(self):
        self.assertEqual(preparation('蓮台寺:1001',3,5), dict(S_SHEET=2,S_DUVET=2,PILLOW=2,MAT=1,FACE=9,BATH=9))
        self.assertEqual(preparation('蓮台寺:1001',1,2)['FACE'], 2)

    def test_t05_queen(self):
        self.assertEqual(preparation('桜町:702',2,1), dict(Q_SHEET=1,Q_DUVET=1,PILLOW=2,MAT=1,FACE=2,BATH=2))
        self.assertIn('S_SHEET', preparation('桜町:804',2,1))

    def test_t06_t07_room_profiles(self):
        for room, expected in [('南熊本:802',dict(S_SHEET=2,S_DUVET=2,SD_SHEET=1,SD_DUVET=1,PILLOW=4)),
                               ('南熊本:803',dict(SD_SHEET=2,SD_DUVET=2,PILLOW=4)),
                               ('八代:101',dict(S_SHEET=2,S_DUVET=2,PILLOW=2)),
                               ('八代:104',dict(SD_SHEET=2,SD_DUVET=2,PILLOW=4))]:
            use=preparation(room,2,1)
            for item,value in expected.items():
                self.assertEqual(use[item], value)

    def test_t08_missing_next_only_towels_unknown(self):
        use = preparation('蓮台寺:1001')
        self.assertEqual(use['S_SHEET'],2)
        self.assertIsNone(use['FACE'])
        self.assertEqual(use['MAT'],1)

    def test_t09_t10_additional_and_no_negative_return(self):
        base = preparation('蓮台寺:1001',confirmed_no_next=True)
        additional = additional_preparation(preparation('蓮台寺:1001',3,2),base)
        self.assertEqual(additional['quantities']['FACE'],4)
        self.assertEqual(additional['quantities']['S_SHEET'],0)
        less = additional_preparation({'FACE':1},{'FACE':2})
        self.assertEqual(less['quantities']['FACE'],0)
        self.assertTrue(less['unknowns'])

    def test_cleaning_uses_next_booking_not_current(self):
        first = row(**{'チェックイン':'2026-10-20','チェックアウト':'2026-10-22','ゲスト数':'1'})
        second = row(**{'チェックイン':'2026-10-24','チェックアウト':'2026-10-29','ゲスト数':'3','合計日数':'5'})
        result=build_preparations(parsed([first,second]),'2026-10-22')
        self.assertEqual(len(result['events']),1)
        self.assertEqual(result['events'][0]['quantities']['FACE'],9)
        self.assertEqual(result['events'][0]['service_date'],'2026-10-22')

    def test_overlap_and_prediction_expiry(self):
        p=parsed([row(),row(**{'チェックイン':'2026-10-1','チェックアウト':'2026-10-3'})])
        self.assertEqual(build_preparations(p,'2026-10-02')['events'],[])
        self.assertEqual(build_preparations(parsed([row()]),'2026-11-01')['status'],'HELD')

    def test_t11_t12_report_order_and_nulls(self):
        report=parse_stock_sequence('蓮台寺:1002','3/4/8/')
        self.assertEqual(report['quantities']['PILLOW'],8)
        self.assertIsNone(report['quantities']['FACE'])
        self.assertEqual(parse_stock_sequence('二本木:','7/5/11/19/22/4/sd4/sd3')['sheet_order'],[7,4,5,3,11,19,22,4])
        self.assertEqual(parse_stock_sequence('桜町:702','q3/q3/4/5/4/2')['sheet_order'],[3,3,4,5,4,2])
        with self.assertRaises(ValueError): parse_stock_sequence('南熊本:802','7/4/5/3/11/19/22/4')

    def test_t13_reconciliation_idempotence(self):
        a=reconcile(13,8,6)
        self.assertEqual(a['after'],11)
        b=reconcile(a['after'],8+a['delta'],6)
        self.assertEqual(b['delta'],0)
        self.assertEqual(reconcile(1,3,0)['after'],-2)
        self.assertIsNone(reconcile(1,3,None)['delta'])

    def test_t15_t17_entry(self):
        stays=[dict(room='細工町:202',ci='2026-10-20',co='2026-10-24',cancelled=False),
               dict(room='細工町:602',ci='2026-10-20',co='2026-10-22',cancelled=False)]
        self.assertFalse(entry_window('細工町:202','2026-10-22',stays)['allowed'])
        self.assertTrue(entry_window('細工町:202','2026-10-22',stays,permission=True)['allowed'])
        self.assertEqual(entry_window('細工町:201','2026-10-22',stays)['priority'],2)
        self.assertEqual(entry_window('細工町:202','2026-10-24',stays)['priority'],1)

    def test_target_and_unknown_stock(self):
        result=target_delivery('新屋敷:701',{'SD_SHEET':0},{'SD_SHEET':1})
        self.assertEqual(result['quantities']['SD_SHEET'],4)
        self.assertIsNone(result['quantities']['FACE'])
        self.assertEqual(target_delivery('八代:101',{}, {})['status'],'HELD')

    def test_t21_order_enumeration(self):
        result=supplier_order('PILLOW',6,20,10)
        self.assertEqual(result['quantity'],50)
        self.assertEqual(result['end_stock'],36)
        self.assertNotIn(100,result['candidates'])

    def test_t22_t23_t24_infeasible(self):
        self.assertEqual(supplier_order('S_SHEET',0,0,31)['status'],'ORDER_INFEASIBLE')
        self.assertEqual(supplier_order('S_SHEET',0,60,0)['status'],'ORDER_INFEASIBLE')
        r=supplier_order('Q_SHEET',13,0,0)
        self.assertEqual(r['existing_over_capacity'],3)
        self.assertIsNone(r['quantity'])

    def test_t25_t26_event_time_shortage(self):
        r=simulate_stock({'FACE':0},[dict(id='use',date='2026-10-22',sequence=1,kind='USE',quantities={'FACE':2}),
                                   dict(id='receipt',date='2026-10-22',sequence=2,kind='RECEIPT',quantities={'FACE':20})])
        self.assertEqual(r['stock']['FACE'],18)
        self.assertEqual(r['shortages'][0]['shortage'],2)
        self.assertFalse(simulate_stock({'FACE':0},[])['shortages'])

    def test_t27_fixed_order_preserved(self):
        r=supplier_order('S_SHEET',0,10,0,fixed=40)
        self.assertEqual(r['quantity'],40)
        self.assertEqual(supplier_order('S_SHEET',0,0,0,fixed=40)['status'],'FIXED_ORDER_CONFLICT')

    def test_t29_t30_yatsushiro_transfers_and_conservation(self):
        donors=[dict(room='八代:103',entry_confirmed=False,stock={'FACE':10},reserved={'FACE':5})]
        r=yatsushiro_supply('八代:106',{'FACE':8},{'FACE':1},donors,destination_entry_confirmed=True)
        self.assertEqual(r['minimum_supplier_need']['FACE'],7)
        donors[0]['entry_confirmed']=True
        r=yatsushiro_supply('八代:106',{'FACE':8},{'FACE':1},donors,destination_entry_confirmed=True)
        self.assertEqual(r['minimum_supplier_need']['FACE'],2)
        a,b=transfer_balances({'FACE':10},{'FACE':1},'FACE',5,True,True)
        self.assertEqual(a['FACE']+b['FACE'],11)
        self.assertEqual(a['FACE'],5)

    def test_delivery_weekdays(self):
        self.assertEqual(next_receipt('2026-10-23'),'2026-10-26')
        self.assertEqual(next_receipt('2026-10-25'),'2026-10-26')
        self.assertEqual(next_receipt('2026-10-26',True),'2026-10-26')


class PersistenceAndIntegrationTests(unittest.TestCase):
    def test_t31_repeat_run_archive_and_backup_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp)
            data=csv_bytes([row()])
            a,reused=run_csv(store,data,'2026-10','full','2026-10-22')
            b,reused=run_csv(store,data,'2026-10','full','2026-10-22')
            self.assertTrue(reused)
            self.assertEqual(a,b)
            self.assertEqual(len(store.list_runs()),1)
            self.assertEqual((Path(tmp)/'sources'/(hashlib.sha256(data).hexdigest()+'.csv')).read_bytes(),data)
            backup=Path(tmp)/'backup.sqlite3'
            store.backup(backup)
            with sqlite3.connect(backup) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0],1)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_t43_slack_replay_and_edit_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp)
            self.assertTrue(store.accept_event('event1',{'ts':'123','text':'v1'}))
            self.assertFalse(store.accept_event('event1',{'ts':'123','text':'v1'}))
            self.assertTrue(store.accept_event('event2',{'ts':'123','text':'v2','edited':True}))

    def test_slack_signature_and_expiry(self):
        body=b'{"event_id":"1"}'
        signature='v0='+hmac.new(b'secret',b'v0:1000:'+body,hashlib.sha256).hexdigest()
        self.assertTrue(verify_slack_signature('secret','1000',signature,body,now=1001))
        self.assertFalse(verify_slack_signature('secret','1000',signature,body,now=1400))
        self.assertFalse(verify_slack_signature('secret','1000',signature,b'{}',now=1001))

    def snapshot(self,month='2026-10'):
        cells={c+'5':dict(value=h) for c,h in MASTER['pl_headers'].items()}
        cells.update({c+str(r):dict(value=0,ownership='SYSTEM_PL_INPUT') for c in MASTER['pl_headers'] for r in (6,8,9,24,25,27)})
        for r in (6,8,9,24,25,27):
            cells['AK'+str(r)]=dict(formula=f'=SUM(B{r}:{"AG" if month=="2026-10" else "AH"}{r})' if r==27 else f'=SUM(B{r}:AJ{r})')
        return dict(tab='OCT2026' if month=='2026-10' else 'NOV2026',sheet_id=50055757 if month=='2026-10' else 54662479,cells=cells)

    def test_t38_t40_snapshot_month_specific_formulas_and_no_mutation(self):
        result=aggregate_pl(parsed([row()]),'2026-10','full')
        snap=self.snapshot()
        before=json.dumps(snap)
        preview=preview_pl(snap,result,'2026-10')
        self.assertFalse(preview['applied'])
        self.assertEqual(json.dumps(snap),before)
        snap['cells']['B5']['value']='Changed'
        with self.assertRaises(ProjectionError): preview_pl(snap,result,'2026-10')
        snap=self.snapshot('2026-11')
        november=aggregate_pl(parsed([row(**{'チェックイン':'2026-10-31','チェックアウト':'2026-11-2'})]),'2026-11','full')
        with self.assertRaises(ProjectionError): preview_pl(snap,result,'2026-11')
        preview_pl(snap,november,'2026-11')
        snap['cells']['AK27']['formula']='=SUM(B27:AG27)'
        with self.assertRaises(ProjectionError): preview_pl(snap,november,'2026-11')

    def test_formula_and_legacy_cells_protected(self):
        result=aggregate_pl(parsed([row()]),'2026-10','full')
        for change in ({'formula':'=1'}, {'ownership':'LEGACY_UNCLASSIFIED'}, {'protected':True}):
            snap=self.snapshot()
            snap['cells']['B6'].update(change)
            with self.assertRaises(ProjectionError): preview_pl(snap,result,'2026-10')

    def test_t41_t42_unknown_write_status_readback(self):
        changes=[dict(cell='B6',before=0,after=100),dict(cell='C6',before=0,after=200)]
        self.assertEqual(verify_readback(changes,{'B6':100,'C6':200}),'APPLIED')
        self.assertEqual(verify_readback(changes,{'B6':0,'C6':0}),'NOT_APPLIED')
        self.assertEqual(verify_readback(changes,{'B6':100,'C6':0}),'WRITE_UNCERTAIN')


if __name__ == '__main__':
    unittest.main()
