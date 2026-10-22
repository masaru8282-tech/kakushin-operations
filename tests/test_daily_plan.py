from copy import deepcopy
from datetime import date
from pathlib import Path
import json
import unittest
from kakushin.daily_plan import schedule_snapshot,schedule_files
from kakushin.master import ITEMS
from kakushin.linen import ORDER_RULES
from kakushin.planning import towel_reduction
from xlsx_fixture import fixtures,workbook


def snapshot(room='蓮台寺:1001'):
    from kakushin.master import MASTER
    days=['2026-10-23','2026-10-24','2026-10-25','2026-10-26']
    keys=list(MASTER['rooms'][room]['rows'])
    return dict(report_days=days[:3],days=days,rooms={room:dict(opening={i:20 for i in keys},days={d:dict(usage={i:0 for i in keys},incoming={i:0 for i in keys}) for d in days})},
        warehouse=dict(opening={i:min(20, ORDER_RULES[i][0]) for i in ITEMS},days={d:dict(receipts={i:0 for i in ITEMS},recorded={i:True for i in ITEMS}) for d in days}),
        yatsushiro_orders={d:{} for d in days})


class DailyPlanTests(unittest.TestCase):
    def run_plan(self,s,options=None,parsed=None):
        return schedule_snapshot(s,parsed or dict(issues=[],reservations=[]),date(2026,10,23),options)

    def test_three_days_include_empty_days_and_do_not_mutate_source(self):
        s=snapshot();before=deepcopy(s)
        r=self.run_plan(s)
        self.assertEqual(len(r['recorded_by_date']),3)
        self.assertIn('2026-10-25',r['line_text'])
        self.assertIn('搬入記載なし',r['line_text'])
        self.assertEqual(s,before)

    def test_recorded_movements_preserved_and_separate_regions(self):
        s=snapshot('八代:106')
        s['rooms']['八代:106']['days']['2026-10-23']['incoming']['SD_SHEET']=20
        s['yatsushiro_orders']['2026-10-23']['SD_SHEET']=20
        r=self.run_plan(s)
        self.assertEqual(r['warehouse']['dispatch']['2026-10-23']['SD_SHEET'],0)
        self.assertEqual(r['yatsushiro'][0]['direct'],20)
        self.assertEqual(r['yatsushiro'][0]['unallocated'],0)
        self.assertIn('SDシーツ 20',r['yatsushiro_text'])
        self.assertNotIn('八代:106',r['line_text'])

    def test_unknown_order_preserves_recorded_and_disables_copy(self):
        s=snapshot();s['warehouse']['days']['2026-10-23']['receipts']['FACE']=20
        r=self.run_plan(s);o=r['orders'][0]
        face=next(i for i in o['items'] if i['item']=='FACE')
        self.assertEqual(face['quantity'],20)
        self.assertEqual(face['candidate'],40)
        self.assertFalse(o['copy_ready'])
        self.assertIn('発注状態未確認',o['text'])

    def test_unordered_draft_has_all_ten_items_and_fixed_orders_do_not_change(self):
        s=snapshot();s['warehouse']['days']['2026-10-23']['receipts']['FACE']=20
        options=dict(order_states={'2026-10-23':'UNORDERED'})
        r=self.run_plan(s,options);o=r['orders'][0]
        self.assertEqual(len(o['items']),10);self.assertTrue(o['copy_ready'])
        self.assertEqual(next(i for i in o['items'] if i['item']=='FACE')['quantity'],40)
        options['order_states']['2026-10-23']='ORDERED'
        r=self.run_plan(s,options);o=r['orders'][0]
        self.assertEqual(next(i for i in o['items'] if i['item']=='FACE')['quantity'],20)
        self.assertIn('発注済み数量保持',o['text'])

    def test_unknown_fixed_item_and_capacity_conflicts_hold_copy(self):
        s=snapshot();s['warehouse']['days']['2026-10-23']['recorded']['FACE']=False
        r=self.run_plan(s,dict(order_states={'2026-10-23':'ORDERED'}))
        self.assertFalse(r['orders'][0]['copy_ready'])
        s['warehouse']['days']['2026-10-23']['receipts']['S_SHEET']=60
        r=self.run_plan(s,dict(order_states={'2026-10-23':'ORDERED'}))
        item=next(i for i in r['orders'][0]['items'] if i['item']=='S_SHEET')
        self.assertEqual(item['quantity'],60)
        self.assertEqual(item['status'],'FIXED_ORDER_CONFLICT')

    def test_intraday_before_receipt_risk_even_if_daily_balance_positive(self):
        s=snapshot();s['warehouse']['opening']['S_SHEET']=0
        s['warehouse']['days']['2026-10-23']['receipts']['S_SHEET']=20
        s['rooms']['蓮台寺:1001']['days']['2026-10-23']['incoming']['S_SHEET']=2
        r=self.run_plan(s)
        self.assertEqual(r['warehouse']['trace'][0]['after']['S_SHEET'],18)
        self.assertEqual(r['warehouse']['timing_risks'][0]['shortage_before_receipt'],2)
        self.assertFalse(self.run_plan(s,dict(receipt_before_dispatch_confirmed=True))['warehouse']['timing_risks'])

    def test_unknown_inventory_is_not_zero_and_known_stay_forbids_entry(self):
        s=snapshot();s['rooms']['蓮台寺:1001']['opening']['FACE']=None
        s['rooms']['蓮台寺:1001']['days']['2026-10-23']['incoming']['FACE']=2
        stay=dict(room='蓮台寺:1001',ci='2026-10-20',co='2026-10-25',cancelled=False,row=1,guests=1,nights=5)
        r=self.run_plan(s,parsed=dict(issues=[],reservations=[stay]))
        self.assertIsNone(r['room_forecast']['蓮台寺:1001'][0]['after']['FACE'])
        self.assertFalse(r['entry_checks'][0]['allowed'])
        self.assertFalse(r['additional_suggestions'])

    def test_additional_target_subtracts_existing_delivery_and_never_overwrites(self):
        s=snapshot();room=s['rooms']['蓮台寺:1001'];room['opening']['S_SHEET']=1
        room['days']['2026-10-23']['incoming']['S_SHEET']=1
        room['days']['2026-10-23']['usage']['S_SHEET']=3
        stay=dict(room='蓮台寺:1001',ci='2026-10-20',co='2026-10-23',cancelled=False,row=1,guests=1,nights=3)
        r=self.run_plan(s,dict(reservation_scope_confirmed=True),dict(issues=[],reservations=[stay]))
        self.assertEqual(r['additional_suggestions'][0]['quantities']['S_SHEET'],7)
        self.assertEqual(r['recorded_by_date']['2026-10-23'][0]['quantities']['S_SHEET'],1)
        self.assertEqual(r['warehouse']['dispatch']['2026-10-23']['S_SHEET'],1)
        self.assertFalse(self.run_plan(s)['additional_suggestions'])

    def test_unknown_towel_slack_does_not_reduce_or_crash(self):
        plans=[dict(id='x',date='2026-10-23',room='蓮台寺:1001',quantities={'FACE':100,'BATH':100})]
        r=towel_reduction(plans,{'x':{'FACE':None,'BATH':0}})
        self.assertEqual(r['plans'][0]['quantities']['FACE'],100)

    def test_files_validate_structure_and_keep_private_rules_unused(self):
        data=Path('examples/reservations.csv').read_bytes();_,s=fixtures(data)
        r=schedule_files(data,workbook(s),'2026-10-23')
        self.assertEqual(r['written_ranges'],[]);self.assertFalse(r['external_sent']);self.assertFalse(r['saved_to_server'])
        self.assertNotIn('チャンネル予約ID',json.dumps(r,ensure_ascii=False))
        with self.assertRaises(ValueError):schedule_files(data,workbook(s),'2026-11-01')
        with self.assertRaises(ValueError):schedule_files(data,workbook(s),'2026-10-23',dict(order_states={'2026-10-24':'ORDERED'}))

    def test_month_boundary_is_unavailable_not_zero(self):
        data=Path('examples/reservations.csv').read_bytes();_,s=fixtures(data)
        r=schedule_files(data,workbook(s),'2026-10-31')
        self.assertEqual(r['unavailable_dates'],['2026-11-01','2026-11-02'])
        self.assertIn('予測期限外・計画未作成',r['line_text'])
        self.assertEqual(r['orders'],[])
        earlier=schedule_files(data,workbook(s),'2026-10-30')
        self.assertFalse(earlier['orders'][0]['copy_ready'])
