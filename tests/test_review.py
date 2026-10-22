from pathlib import Path
import hashlib
import json
import unittest
from zipfile import ZipFile
from io import BytesIO
from unittest.mock import patch
from kakushin.xlsx import Xlsx,translate_formula
from kakushin.review import review_files
from xlsx_fixture import workbook,fixtures

DATA=Path('examples/reservations.csv').read_bytes()


class ReadOnlyReviewTests(unittest.TestCase):
    def setUp(self):self.pl,self.linen=fixtures(DATA)
    def run_review(self,approvals=None,scope='full',data=DATA):
        return review_files(data,workbook({'OCT2026':self.pl}),workbook(self.linen),'2026-10','2026-10-23',scope,approvals)

    def test_pl_matches_and_known_usage_differences_are_not_hidden(self):
        r=self.run_review()
        self.assertEqual(r['PL']['matched'],210)
        self.assertEqual(r['PL']['status'],'MATCH')
        self.assertEqual(r['written_ranges'],[])
        self.assertNotIn('予約ID',json.dumps(r,ensure_ascii=False))

    def test_scope_unknown_holds_even_if_numbers_match(self):
        self.assertEqual(self.run_review(scope='unknown')['PL']['status'],'HELD')

    def test_count_exemption_is_per_run_and_only_ai_aj(self):
        self.pl['AI8']+=2;self.pl['AJ8']+=1;self.pl['AK8']['value']+=3
        r=self.run_review();self.assertEqual(r['PL']['status'],'DIFFERENCES')
        a=dict(source_sha256=hashlib.sha256(DATA).hexdigest(),ignore_yatsushiro_counts=True,count_reason='Fictional single-run exception')
        r=self.run_review(a)
        self.assertEqual(r['PL']['status'],'MATCH');self.assertEqual(r['PL']['ignored_count'],2)
        self.assertEqual(self.run_review()['PL']['status'],'DIFFERENCES')
        self.pl['B6']+=1;self.pl['AK6']['value']+=1
        self.assertEqual(self.run_review(a)['PL']['status'],'DIFFERENCES')

    def test_file_hash_mismatch_and_missing_reason_rejected(self):
        for a in (dict(source_sha256='wrong'),dict(source_sha256=hashlib.sha256(DATA).hexdigest(),ignore_yatsushiro_counts=True)):
            with self.assertRaises(ValueError):self.run_review(a)

    def test_changed_mapping_or_formula_is_not_silently_used(self):
        self.pl['B5']='Unexpected'
        with self.assertRaises(ValueError):self.run_review()
        self.pl,self.linen=fixtures(DATA);self.pl['AK27']['formula']='SUM(B27:AH27)'
        with self.assertRaises(ValueError):self.run_review()
        self.pl,self.linen=fixtures(DATA);self.linen['部屋使用']['A2']='Wrong room'
        with self.assertRaises(ValueError):self.run_review()

    def test_negative_unknown_and_unallocated_remain_separate(self):
        self.linen['現在の在庫']['X259']=-1
        self.linen['現在の在庫'].pop('X260')
        self.linen['八代業者入庫履歴']['X4']=10
        r=self.run_review()
        self.assertTrue(r['linen']['negative_stock']);self.assertTrue(r['linen']['unknown_stock'])
        self.assertTrue(r['linen']['yatsushiro_unallocated']);self.assertEqual(r['PL']['status'],'MATCH')

    def test_private_config_not_loaded(self):
        with patch('kakushin.csvio.load_exceptions',side_effect=AssertionError('private config read')):
            self.run_review()

    def test_shared_formulas_and_strings(self):
        cells={'A1':dict(value=0,type='s'),'AK8':dict(value=2,formula='SUM(B8:AJ8)',formula_attrs=' t="shared" si="0"'),
               'AK9':dict(value=7,formula='',formula_attrs=' t="shared" si="0"')}
        w=Xlsx(workbook({'OCT2026':cells},shared=['Synthetic header']))
        s=w.sheet('OCT2026',28,37)
        self.assertEqual(s['A1']['value'],'Synthetic header')
        self.assertEqual(s['AK9']['formula'],'SUM(B9:AJ9)')
        self.assertEqual(translate_formula('SUM($B8:AJ$9)+"A1"','AK8','AL9'),'SUM($B9:AK$9)+"A1"')

    def test_no_formula_recalculation_and_bad_zip_rejected(self):
        w=Xlsx(workbook({'OCT2026':{'A1':dict(value=None,formula='1+1')}}))
        self.assertIsNone(w.sheet('OCT2026',1,1)['A1']['value'])
        with self.assertRaises(ValueError):Xlsx(b'not a zip')

    def test_external_entity_xml_rejected(self):
        data=BytesIO()
        with ZipFile(data,'w') as z:z.writestr('xl/workbook.xml','<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]><workbook/>')
        with self.assertRaises(ValueError):Xlsx(data.getvalue())

    def test_profile_is_bound_to_excel_as_well_as_csv(self):
        a=dict(source_sha256=hashlib.sha256(DATA).hexdigest(),pl_sha256='different-file')
        with self.assertRaises(ValueError):self.run_review(a)
