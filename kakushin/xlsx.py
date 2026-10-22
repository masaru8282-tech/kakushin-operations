"""Bounded, read-only XLSX reader. Never evaluates formulas or follows links."""
from datetime import date, timedelta
from io import BytesIO
import math
import posixpath
import re
from zipfile import ZipFile, BadZipFile
import xml.etree.ElementTree as ET

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
S = '{' + NS['s'] + '}'


def column(number):
    out = ''
    while number:
        number, rem = divmod(number-1, 26)
        out = chr(65+rem)+out
    return out


class Xlsx:
    def __init__(self, data):
        if len(data) > 12*1024*1024:
            raise ValueError('Excelファイルは12MB以下にしてください')
        try:
            self.archive = ZipFile(BytesIO(data))
            members = self.archive.infolist()
            names = [m.filename for m in members]
            if len(names) != len(set(names)) or len(names)>2000:
                raise ValueError('Excel ZIPの構造が不正です')
            if sum(m.file_size for m in members)>128*1024*1024 or any(m.file_size>16*1024*1024 for m in members):
                raise ValueError('Excel展開サイズが上限を超えています')
            if any(m.flag_bits & 1 for m in members) or any('vbaproject' in n.lower() for n in names):
                raise ValueError('暗号化・マクロ付きExcelは扱えません')
            workbook = self.xml('xl/workbook.xml')
            prop=workbook.find('s:workbookPr', NS)
            self.epoch = date(1904,1,1) if prop is not None and prop.get('date1904') in ('1','true') else date(1899,12,30)
            rels = self.xml('xl/_rels/workbook.xml.rels')
            targets={r.get('Id'):r.get('Target') for r in rels if r.get('TargetMode')!='External'}
            self.paths={}
            for sheet in workbook.findall('s:sheets/s:sheet',NS):
                target=targets.get(sheet.get('{'+NS['r']+'}id'),'')
                path=posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
                if not path.startswith('xl/worksheets/'):
                    continue
                self.paths[sheet.get('name')]=path
            self.strings=[]
            if 'xl/sharedStrings.xml' in names:
                shared=self.xml('xl/sharedStrings.xml')
                self.strings=[''.join(e.itertext()) if not e.findall('.//s:t',NS) else ''.join(t.text or '' for t in e.findall('.//s:t',NS)) for e in shared]
        except (BadZipFile, KeyError, ET.ParseError, OSError, RuntimeError):
            raise ValueError('Excelを読み取れません。.xlsx形式で書き出してください') from None

    def xml(self, path):
        raw=self.archive.read(path)
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('外部参照を含むXMLは扱えません')
        return ET.fromstring(raw)

    def sheet(self, name, max_row, max_col):
        if name not in self.paths:
            raise ValueError(f'必要なタブがありません: {name}')
        result={};shared={}
        try:
            for cell in self.xml(self.paths[name]).iter(S+'c'):
                address=cell.get('r','');m=re.fullmatch(r'([A-Z]+)([1-9][0-9]*)',address)
                if not m:continue
                formula=cell.find(S+'f')
                if formula is not None and formula.get('t')=='shared' and formula.text:
                    shared[formula.get('si')]=(address,formula.text)
                n=0
                for ch in m[1]:n=n*26+ord(ch)-64
                if int(m[2])>max_row or n>max_col:continue
                t=cell.get('t','n');v=cell.find(S+'v');formula=cell.find(S+'f')
                raw=None if v is None else v.text
                if t=='s':value=self.strings[int(raw)] if raw is not None else None
                elif t=='inlineStr':value=''.join(e.text or '' for e in cell.findall('.//s:t',NS))
                elif t in ('str','e','d'):value=raw
                elif t=='b':value=raw=='1'
                elif raw is None:value=None
                else:
                    value=float(raw)
                    if not math.isfinite(value):raise ValueError('Excelに不正な数値があります')
                    if value.is_integer():value=int(value)
                result[address]={'value':value,'formula':formula.text if formula is not None else None,
                                 'has_formula':formula is not None,'type':t,
                                 '_shared':formula.get('si') if formula is not None and formula.get('t')=='shared' else None}
        except (KeyError,IndexError,ET.ParseError,TypeError,OverflowError):
            raise ValueError('Excelのセル形式を確認してください') from None
        for address,cell in result.items():
            if cell['_shared'] is not None and not cell['formula']:
                if cell['_shared'] not in shared:raise ValueError('共有数式の元セルがありません')
                source,expression=shared[cell['_shared']]
                cell['formula']=translate_formula(expression,source,address)
            cell.pop('_shared',None)
        return result

    def date(self,value):
        if isinstance(value,(int,float)) and not isinstance(value,bool):
            return (self.epoch+timedelta(days=int(value))).isoformat()
        if isinstance(value,str):
            try:return date.fromisoformat(value[:10]).isoformat()
            except ValueError:pass
        raise ValueError('日付見出しの型を確認してください')

    def close(self):
        self.archive.close()


def value(cells,address):
    return cells.get(address,{}).get('value')


def translate_formula(expression,source,destination):
    def coordinates(address):
        m=re.fullmatch(r'([A-Z]+)([0-9]+)',address);c=0
        for ch in m[1]:c=c*26+ord(ch)-64
        return c,int(m[2])
    sc,sr=coordinates(source);dc,dr=coordinates(destination)
    def replace(match):
        col,row=match.groups();absolute_col=col.startswith('$');absolute_row=row.startswith('$')
        c,r=coordinates(col.lstrip('$')+row.lstrip('$'))
        c=c if absolute_col else c+dc-sc;r=r if absolute_row else r+dr-sr
        if c<1 or r<1:raise ValueError('共有数式の参照範囲が不正です')
        return ('$' if absolute_col else '')+column(c)+('$' if absolute_row else '')+str(r)
    parts=re.split(r'("(?:[^"]|"")*")',expression)
    for n in range(0,len(parts),2):
        parts[n]=re.sub(r'(?<![A-Za-z0-9_])(\$?[A-Z]{1,3})(\$?[1-9][0-9]*)(?![A-Za-z0-9_])',replace,parts[n])
    return ''.join(parts)
