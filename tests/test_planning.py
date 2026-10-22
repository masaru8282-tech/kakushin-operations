import unittest
from kakushin.planning import draft_plan, towel_reduction


class PlanningTests(unittest.TestCase):
    def test_t19_t20_towel_reduction_only_safe_surplus(self):
        plans=[dict(id='a',room='細工町:202',date='2026-10-22',quantities={'FACE':100,'BATH':100})]
        r=towel_reduction(plans,{'a':{'FACE':10,'BATH':4}})
        self.assertEqual(r['reduction']['FACE']['final'],90)
        self.assertEqual(r['reduction']['BATH']['final'],96)
        self.assertEqual(r['reduction']['BATH']['rate'],4)
        self.assertEqual(plans[0]['quantities']['FACE'],100)

    def test_three_days_and_horizon_target(self):
        room='細工町:202'
        stock={room:dict(S_SHEET=0,S_DUVET=0,PILLOW=1,FACE=1,BATH=1,MAT=0)}
        uses=[dict(room=room,date='2026-10-22',quantities=dict(S_SHEET=2,S_DUVET=2,PILLOW=2,FACE=2,BATH=2,MAT=1))]
        stays=[dict(room=room,ci='2026-10-20',co='2026-10-22',cancelled=False)]
        r=draft_plan('2026-10-22','2026-10-24',stock,uses,stays,True)
        self.assertEqual(len(r['plans_by_date']),3)
        self.assertEqual(r['plans_by_date']['2026-10-22'][0]['quantities']['S_SHEET'],8)
        self.assertEqual(r['room_forecast'][room][0]['stock']['S_SHEET'],6)
        self.assertEqual(r['status'],'CANDIDATE_NOT_CONFIRMED')

    def test_entry_unconfirmed_or_staying_cannot_plan(self):
        room='細工町:202'
        stock={room:dict(S_SHEET=0,S_DUVET=0,PILLOW=1,FACE=1,BATH=1,MAT=0)}
        uses=[dict(room=room,date='2026-10-22',quantities={'S_SHEET':2})]
        stays=[dict(room=room,ci='2026-10-20',co='2026-10-24',cancelled=False)]
        r=draft_plan('2026-10-22','2026-10-24',stock,uses,stays,True)
        self.assertFalse(any(r['plans_by_date'].values()))
        self.assertTrue(r['shortages'])

    def test_timed_warehouse_dispatch_is_validated(self):
        room='細工町:202'
        stock={room:dict(S_SHEET=0,S_DUVET=0,PILLOW=1,FACE=1,BATH=1,MAT=0)}
        uses=[dict(room=room,date='2026-10-22',quantities={'S_SHEET':2})]
        stays=[dict(room=room,ci='2026-10-20',co='2026-10-22',cancelled=False)]
        warehouse={'opening':{'S_SHEET':0},'events':[dict(id='late-receipt',date='2026-10-22',sequence=3,kind='RECEIPT',quantities={'S_SHEET':20})],
                   'dispatch_sequence':{'2026-10-22':2}}
        r=draft_plan('2026-10-22','2026-10-24',stock,uses,stays,True,warehouse=warehouse)
        self.assertEqual(r['warehouse']['shortages'][0]['shortage'],8)

    def test_yatsushiro_not_dispatched_from_kumamoto(self):
        r=draft_plan('2026-10-22','2026-10-24',{},[dict(room='八代:106',date='2026-10-22',quantities={'SD_SHEET':2})],[],True)
        self.assertFalse(any(r['normal_dispatch']['2026-10-22'].values()))
        self.assertTrue(r['unknowns'])

    def test_shorter_than_three_day_horizon_rejected(self):
        with self.assertRaises(ValueError):
            draft_plan('2026-10-22','2026-10-23',{},[],[])

    def test_expired_linen_rule_range_rejected(self):
        with self.assertRaises(ValueError):
            draft_plan('2026-10-30','2026-11-02',{},[],[])
