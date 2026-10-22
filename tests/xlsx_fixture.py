"""Fictional small XLSX fixtures: no real guests, bookings or workbook data."""
from io import BytesIO
from zipfile import ZipFile
from xml.sax.saxutils import escape
from datetime import date,timedelta
from kakushin.master import MASTER,ITEMS
from kakushin.xlsx import column
from kakushin.engine import aggregate_pl
from kakushin.csvio import parse_csv


def workbook(sheets,shared=None):
    buffer=BytesIO()
    with ZipFile(buffer,'w') as z:
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="r{n}"/>' for n,name in enumerate(sheets,1))+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="r{n}" Target="worksheets/sheet{n}.xml"/>' for n in range(1,len(sheets)+1))+'</Relationships>')
        if shared is not None:z.writestr('xl/sharedStrings.xml','<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'+''.join('<si><t>'+escape(v)+'</t></si>' for v in shared)+'</sst>')
        for n,(name,cells) in enumerate(sheets.items(),1):
            out=[]
            for address,obj in cells.items():
                if isinstance(obj,dict):v=obj.get('value');formula=obj.get('formula');attrs=obj.get('formula_attrs','');t=obj.get('type')
                else:v=obj;formula=None;attrs='';t=None
                if formula is not None:
                    out.append(f'<c r="{address}"><f{attrs}>{escape(formula)}</f>'+('' if v is None else '<v>'+str(v)+'</v>')+'</c>')
                elif t=='s':out.append(f'<c r="{address}" t="s"><v>{v}</v></c>')
                elif isinstance(v,str):out.append(f'<c r="{address}" t="inlineStr"><is><t>{escape(v)}</t></is></c>')
                elif v is not None:out.append(f'<c r="{address}"><v>{v}</v></c>')
            z.writestr(f'xl/worksheets/sheet{n}.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row>'+''.join(out)+'</row></sheetData></worksheet>')
    return buffer.getvalue()


def fixtures(csv_data):
    from kakushin.review import ALIASES
    result=aggregate_pl(parse_csv(csv_data,exceptions={}),'2026-10','full')
    cells={c+'5':h for c,h in MASTER['pl_headers'].items()}
    cells.update(result['candidate_cells'])
    for key,row in [('sales',6),('count',8),('nights',9),('booking_fee',24),('airbnb_fee',25),('tax',27)]:
        end='AG' if row==27 else 'AJ'
        cells['AK'+str(row)]=dict(value=result['totals'][key],formula=f'SUM(B{row}:{end}{row})')
    sheets={name:{} for name in ('現在の在庫','部屋使用','部屋入庫','倉庫入出庫履歴','八代業者入庫履歴')}
    for day in range(1,32):
        serial=(date(2026,10,day)-date(1899,12,30)).days
        for name,s in sheets.items():s[f'{column(day+2 if name=="倉庫入出庫履歴" else day+1)}{3 if name=="倉庫入出庫履歴" else 1}']=serial
    for room,m in MASTER['rooms'].items():
        header=min(m['rows'].values())-1
        for name in ('現在の在庫','部屋使用','部屋入庫'):
            sheets[name]['A'+str(header)]=room.replace(':','')
            for item,row in m['rows'].items():
                sheets[name]['A'+str(row)]=next(iter(ALIASES[item]))
                for day in range(1,32):sheets[name][column(day+1)+str(row)]=20 if name=='現在の在庫' else 0
    for day in range(1,32):
        for n,item in enumerate(ITEMS):
            for name in ('部屋使用','部屋入庫'):sheets[name][column(day+1)+str(246+n)]=0
            sheets['倉庫入出庫履歴'][column(day+2)+str(16+n)]=0
            for base in (4,16,28):sheets['八代業者入庫履歴'][column(day+1)+str(base+n)]=0
    return cells,sheets
