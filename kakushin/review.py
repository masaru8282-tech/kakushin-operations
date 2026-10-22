"""Read-only file reconciliation. Approvals apply only to the submitted file hash.

No raw input, guest identities or booking identifiers are persisted or returned.
"""
from collections import Counter
from copy import deepcopy
from datetime import date,timedelta
import hashlib

from .csvio import parse_csv,issue
from .engine import aggregate_pl,build_preparations,PL_ROWS
from .master import MASTER,PL_MAP,ITEMS,LABELS,RULE_VERSION,normalize
from .xlsx import Xlsx,column,value


ALIASES={'S_SHEET':{'シーツ','Sシーツ'},'S_DUVET':{'デュベ','Sデュベ'},
 'PILLOW':{'ピロー','ピロ'},'FACE':{'小','フェイス','フェイスタオル'},
 'BATH':{'大','バス','バスタオル'},'MAT':{'マット','バスマット'},
 'Q_SHEET':{'Qシーツ'},'Q_DUVET':{'Qデュベ'},'SD_SHEET':{'SDシーツ'},'SD_DUVET':{'SDデュベ'}}
METRICS={'sales':'売上','count':'件数','nights':'泊数','booking_fee':'Booking手数料','airbnb_fee':'Airbnb手数料','tax':'宿泊税'}


def date_columns(book,cells,row):
    out={}
    for c in range(2,39):
        v=value(cells,f'{column(c)}{row}')
        if v in (None,''):continue
        try:d=book.date(v)
        except ValueError:continue
        if d in out:raise ValueError('日付見出しが重複しています')
        out[d]=c
    return out


def review_files(csv_data,pl_data,linen_data,month,business_date,scope='unknown',approvals=None):
    start=date.fromisoformat(business_date)
    if start.strftime('%Y-%m')!=month:
        raise ValueError('業務日と対象月を揃えてください')
    parsed=parse_csv(csv_data,exceptions={})
    approvals=approvals or {}
    sha=parsed['sha256']
    if approvals and approvals.get('source_sha256')!=sha:
        raise ValueError('承認したCSVと入力が異なります。新しいファイルを再確認してください')
    for key,want in (('month',month),('business_date',business_date)):
        if approvals.get(key) and approvals[key]!=want:
            raise ValueError('確認済み条件と対象月・業務日が異なります')
    for key,data in (('pl_sha256',pl_data),('linen_sha256',linen_data)):
        if approvals.get(key) and approvals[key]!=hashlib.sha256(data).hexdigest():
            raise ValueError('確認済み条件とExcelが異なります。この実行の条件を再確認してください')
    p=deepcopy(parsed)
    decisions=[]
    manual=bool(approvals.get('include_zero_manual'))
    overrides=approvals.get('cancellations',[])
    if not isinstance(overrides,list):raise ValueError('個別取消料の確認形式が不正です')
    overrides_by_row={o['row']:o for o in overrides}
    if len(overrides_by_row)!=len(overrides):raise ValueError('取消料行の確認が重複しています')
    accepted=set();candidates=[]
    for r in p['reservations']:
        if r['ota']=='手動作成' and not r['cancelled'] and r['sales']==0 and r['fee']==0 and manual:
            accepted.add((r['row'],'未承認OTAの手数料処理'))
            decisions.append(dict(kind='ZERO_MANUAL',row=r['row'],scope='THIS_FILE_ONLY'))
        if r['cancelled'] and (r['sales'] or r['fee']):
            candidates.append(dict(row=r['row'],room=r['room'],ota=r['ota'],sales=r['sales'],fee=r['fee'],
                                   ci=r['ci'],co=r['co'],suggested_column=PL_MAP.get(r['room'])))
            o=overrides_by_row.pop(r['row'],None)
            if o is None:continue
            target=o.get('column');reason=str(o.get('reason','')).strip()
            if target not in MASTER['pl_headers'] or target in ('AF','AH') or not reason:
                raise ValueError('取消料は確認済みのPL列と理由を指定してください')
            known=PL_MAP.get(r['room'])
            if known and target!=known:raise ValueError('施設・部屋と取消料のPL列が一致しません')
            if not known and not (r['property']=='08_vetta@yatsushiro Egretta' and r['room_number']=='' and target in ('AI','AJ')):
                raise ValueError('未対応施設の取消料は自動割当できません')
            if r['ota'] not in ('Airbnb','Booking.com'):raise ValueError('取消料のOTAが未対応です')
            r.update(exception=target,exception_month=month,excluded=False)
            accepted.add((r['row'],'新しい取消料は承認が必要'))
            decisions.append(dict(kind='CANCELLATION_FEE',row=r['row'],column=target,reason=reason,scope='THIS_FILE_ONLY'))
    if overrides_by_row:raise ValueError('確認対象にない取消料行が指定されています')
    p['issues']=[i for i in p['issues'] if (i['row'],i['message']) not in accepted]
    pl_expected=aggregate_pl(p,month,scope)
    result={'mode':'READ_ONLY_REVIEW','source_sha256':sha,'business_date':business_date,'month':month,
            'row_count':len(parsed['reservations']),'rule_version':RULE_VERSION,
            'decisions':decisions,'cancellation_candidates':candidates,
            'zero_manual_candidates':[r['row'] for r in p['reservations'] if r['ota']=='手動作成' and not r['sales'] and not r['fee'] and not r['cancelled']],
            'issues':p['issues'],'written_ranges':[],
            'verification':{'live_sheets':'NOT_CONNECTED','external_slack':'NOT_CONNECTED'},
            'source_files':[{'kind':'CSV','sha256':sha},{'kind':'PL_XLSX','sha256':hashlib.sha256(pl_data).hexdigest()},
                            {'kind':'LINEN_XLSX','sha256':hashlib.sha256(linen_data).hexdigest()}]}
    plbook=Xlsx(pl_data)
    try:result['PL']=compare_pl(plbook,pl_expected,month,approvals)
    finally:plbook.close()
    linenbook=Xlsx(linen_data)
    try:result['linen']=compare_linen(linenbook,p,start)
    finally:linenbook.close()
    result['status']='REVIEW_REQUIRED' if result['PL']['status']!='MATCH' or result['linen']['issues'] else 'MATCH_WITHIN_CHECKED_SCOPE'
    result['limitations']=['書き出し時のExcelキャッシュ値を比較。数式の再計算・ライブシート更新は行いません',
        '次回予約不明・実在庫未報告・入出庫の予定／実績・入室時刻は別確認が必要',
        'アップロード内容・実行結果・今回の承認はサーバーのディスクやGitHubへ保存しません']
    return result


def compare_pl(book,expected,month,approvals):
    name={'2026-10':'OCT2026','2026-11':'NOV2026'}.get(month)
    if not name:raise ValueError('対象月のPLタブ規則は未確認です')
    cells=book.sheet(name,28,37)
    for c,header in MASTER['pl_headers'].items():
        if value(cells,c+'5')!=header:raise ValueError(f'PL見出しが異なります: {c}5')
    for metric,row in PL_ROWS.items():
        formula=cells.get('AK'+str(row),{}).get('formula')
        end=('AG' if month=='2026-10' else 'AH') if row==27 else 'AJ'
        if (formula or '').replace(' ','').upper()!=f'SUM(B{row}:{end}{row})':
            raise ValueError(f'PL合計式が異なります: AK{row}')
    differences=[];missing=[];matches=0
    ignore=approvals.get('ignore_yatsushiro_counts') is True
    reason=str(approvals.get('count_reason','')).strip()
    if ignore and not reason:raise ValueError('今回だけの件数差除外には理由が必要です')
    for address,want in expected['candidate_cells'].items():
        cell=cells.get(address,{})
        got=cell.get('value')
        if cell.get('has_formula') or cell.get('type')=='e':
            missing.append(dict(cell=address,message='入力セルに数式・エラーがあります'))
            continue
        if got==want and type(got) is not bool:
            matches+=1
        else:
            col=''.join(x for x in address if x.isalpha())
            row=int(''.join(x for x in address if x.isdigit()))
            differences.append(dict(cell=address,room=MASTER['pl_headers'][col],metric=METRICS[next(k for k,v in PL_ROWS.items() if v==row)],
                csv=want,xlsx=got,ignored=bool(ignore and address in ('AI8','AJ8')),
                reason=reason if ignore and address in ('AI8','AJ8') else None))
    totals=[]
    for metric,row in PL_ROWS.items():
        got=value(cells,'AK'+str(row))
        if got is None:missing.append(dict(cell='AK'+str(row),message='合計の計算結果が未取得'))
        end=33 if metric=='tax' and month=='2026-10' else 34 if metric=='tax' else 36
        values=[value(cells,f'{column(c)}{row}') for c in range(2,end+1)]
        if any(v is not None and (isinstance(v,bool) or not isinstance(v,(int,float))) for v in values):
            missing.append(dict(cell='AK'+str(row),message='対象範囲に非数値があります'))
        elif got is not None and sum(v or 0 for v in values)!=got:
            missing.append(dict(cell='AK'+str(row),message='合計キャッシュと対象セルの和が異なります'))
        totals.append(dict(metric=METRICS[metric],csv=expected['totals'][metric],xlsx=got))
    blocked=expected['status']=='HELD' or bool(missing)
    return dict(headers=MASTER['pl_headers'],status='HELD' if blocked else 'DIFFERENCES' if any(not d['ignored'] for d in differences) else 'MATCH',
                checked=210,matched=matches,differences=differences,totals=totals,
                ignored_count=sum(d['ignored'] for d in differences),issues=expected['issues']+missing,
                note='未承認・保留がある場合のCSV値は部分集計であり確定値ではありません')


def read_linen(book,start):
    sheets={name:book.sheet(name,303,38) for name in ('現在の在庫','部屋使用','部屋入庫','倉庫入出庫履歴','八代業者入庫履歴')}
    dates={name:date_columns(book,cells,3 if name=='倉庫入出庫履歴' else 1) for name,cells in sheets.items()}
    required=[(start+timedelta(days=i)).isoformat() for i in range(3) if (start+timedelta(days=i)).month==start.month]
    for name in dates:
        if any(d not in dates[name] for d in required):raise ValueError(f'{name}の対象日付がありません')
    facility_context={name:None for name in ('現在の在庫','部屋使用','部屋入庫')}
    for room,m in MASTER['rooms'].items():
        header=min(m['rows'].values())-1
        for name in ('現在の在庫','部屋使用','部屋入庫'):
            label=normalize(value(sheets[name],f'A{header}') or '').replace('_','').replace(' ','')
            facility,number=room.split(':')
            aliases=[facility] if facility!='水前寺②' else ['水前寺②','水前寺2','水2','セルジュ']
            full={normalize(a+number).replace(' ','') for a in aliases}
            if label in full:
                facility_context[name]=facility
            elif not (number and label==number and facility_context[name]==facility):
                raise ValueError(f'{name}の部屋見出しが異なります: A{header}')
            for item,row in m['rows'].items():
                if normalize(value(sheets[name],f'A{row}') or '') not in {normalize(x) for x in ALIASES[item]}:
                    raise ValueError(f'{name}の品目行が異なります: A{row}')
    return sheets,dates


def compare_linen(book,parsed,start):
    sheets,dates=read_linen(book,start)
    stock,use,inc,warehouse,ys=[sheets[n] for n in sheets]
    required=[(start+timedelta(days=i)).isoformat() for i in range(3) if (start+timedelta(days=i)).month==start.month]
    issues=[];usage_diff=[];checked=0;skipped=0;events_count=0
    end=date(2026,10,31)
    # Only the documented October 2026 linen horizon is enabled.
    if start>date(2026,10,31):
        return dict(status='HELD',issues=[dict(kind='SCOPE_UNKNOWN',message='リネンの承認済み予測期限を超えています')],usage_checked=0,usage_differences=[],usage_skipped=0)
    for offset in range(0,(end-start).days+1,3):
        batch=build_preparations(parsed,(start+timedelta(days=offset)).isoformat())
        for event in batch['events']:
            events_count+=1
            for item,q in event['quantities'].items():
                if q is None:skipped+=1;continue
                day=event['service_date'];row=MASTER['rooms'][event['room']]['rows'][item]
                if day not in dates['部屋使用']:
                    skipped+=1;continue
                address=column(dates['部屋使用'][day])+str(row)
                got=value(use,address);checked+=1
                if got!=q:usage_diff.append(dict(room=event['room'],date=day,item=LABELS[item],csv=q,xlsx=got,cell=address))
        if batch['status']=='HELD':issues.extend(dict(kind='USAGE_HELD',message=i['message']) for i in batch['issues'])
    if skipped:issues.append(dict(kind='NEXT_BOOKING_UNKNOWN',message=f'次回予約等が不明な{skipped}品目は使用数の一致判定から除外'))
    recurrence=[];negative=[];unknown=[];links=[];unallocated=[]
    for day in required:
        sc=dates['現在の在庫'][day];previous=(date.fromisoformat(day)-timedelta(days=1)).isoformat()
        for room,m in MASTER['rooms'].items():
            for item,row in m['rows'].items():
                address=column(sc)+str(row);current=value(stock,address)
                if current in (None,''):
                    unknown.append(dict(room=room,date=day,item=LABELS[item],cell=address));continue
                if isinstance(current,(int,float)) and not isinstance(current,bool) and current<0:
                    negative.append(dict(room=room,date=day,item=LABELS[item],quantity=current))
                if all(previous in dates[n] for n in ('現在の在庫','部屋使用','部屋入庫')):
                    prev=value(stock,column(dates['現在の在庫'][previous])+str(row))
                    used=value(use,column(dates['部屋使用'][previous])+str(row))
                    incoming=value(inc,column(dates['部屋入庫'][previous])+str(row))
                    if isinstance(prev,(int,float)) and all(v in (None,'') or isinstance(v,(int,float)) for v in (used,incoming)):
                        want=prev-(used or 0)+(incoming or 0)
                        if current!=want:recurrence.append(dict(room=room,date=day,item=LABELS[item],cell=address,calculated=want,xlsx=current))
        for n,item in enumerate(ITEMS):
            kumarows=[m['rows'][item] for room,m in MASTER['rooms'].items() if item in m['rows'] and not room.startswith('八代:')]
            yrows=[m['rows'][item] for room,m in MASTER['rooms'].items() if item in m['rows'] and room.startswith('八代:')]
            def total(cells,rows,c):
                values=[value(cells,column(c)+str(r)) for r in rows]
                if any(v not in (None,'') and (isinstance(v,bool) or not isinstance(v,(int,float))) for v in values):
                    raise ValueError('入出庫・使用に非数値またはエラーがあります')
                return sum(v or 0 for v in values)
            incoming=total(inc,kumarows,dates['部屋入庫'][day])
            dispatch=value(warehouse,column(dates['倉庫入出庫履歴'][day])+str(16+n))
            if incoming!=dispatch:links.append(dict(kind='WAREHOUSE_DISPATCH',date=day,item=LABELS[item],expected=incoming,xlsx=dispatch))
            for name,cells in (('部屋使用',use),('部屋入庫',inc)):
                want=total(cells,kumarows,dates[name][day]);got=value(cells,column(dates[name][day])+str(246+n))
                if want!=got:links.append(dict(kind='KUMAMOTO_SUM',sheet=name,date=day,item=LABELS[item],expected=want,xlsx=got))
            direct=total(inc,yrows,dates['部屋入庫'][day]);yc=column(dates['八代業者入庫履歴'][day])
            observed=value(ys,yc+str(16+n));ordered=value(ys,yc+str(4+n));remaining=value(ys,yc+str(28+n))
            if observed not in (None,'') or direct:
                if observed!=direct:links.append(dict(kind='YATSUSHIRO_DIRECT',date=day,item=LABELS[item],expected=direct,xlsx=observed))
            if isinstance(ordered,(int,float)):
                pending=ordered-direct
                if pending or remaining!=pending:unallocated.append(dict(date=day,item=LABELS[item],quantity=pending))
    # Check the known formula family even when quantities happen to cancel out.
    known_formulas=[]
    for row in (80,81):
        for day in required:
            c=dates['現在の在庫'][day];address=column(c)+str(row);formula=stock.get(address,{}).get('formula') or ''
            if "'部屋入庫'!"+address in formula:known_formulas.append(dict(cell=address,date=day,room='桜町:804',message='朝在庫が当日入庫を参照'))
    for kind,items in [('USAGE_DIFFERENCE',usage_diff),('STOCK_RECURRENCE',recurrence),('STOCK_NEGATIVE',negative),('STOCK_UNKNOWN',unknown),('LINK_DIFFERENCE',links),('YATSUSHIRO_UNALLOCATED',unallocated),('KNOWN_FORMULA',known_formulas)]:
        if items:issues.append(dict(kind=kind,message=f'{len(items)}件の確認事項'))
    return dict(status='REVIEW_REQUIRED' if issues else 'MATCH_WITHIN_CHECKED_SCOPE',usage_events=events_count,
                usage_checked=checked,usage_skipped=skipped,usage_differences=usage_diff,
                recurrence_differences=recurrence,negative_stock=negative,unknown_stock=unknown,
                link_differences=links,yatsushiro_unallocated=unallocated,known_formulas=known_formulas,issues=issues)
