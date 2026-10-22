"""Three-day linen dashboard from uploaded snapshots; no writes or external sends.

Recorded movements are preserved. Additional deliveries and supplier quantities are
separate proposals. Unknown ownership, timing and actual inventory remain explicit.
"""
from collections import defaultdict
from datetime import date,timedelta
from hashlib import sha256

from .csvio import parse_csv
from .engine import build_preparations
from .linen import entry_window,next_receipt,ORDER_RULES,supplier_order
from .master import MASTER,ITEMS,LABELS,profile
from .planning import towel_reduction
from .review import read_linen
from .xlsx import Xlsx,column,value

LIMIT=date(2026,10,31)


def amount(cells,address,stock=False):
    cell=cells.get(address,{})
    v=cell.get('value')
    if v in (None,''):
        return None if stock or cell.get('has_formula') else 0
    if type(v) is not int or (v<0 and not stock):
        raise ValueError(f'在庫・数量の型を確認してください: {address}')
    return v


def schedule_files(csv_data,linen_data,business_date,options=None):
    options=options or {}
    start=date.fromisoformat(business_date)
    if start.year!=2026 or start.month!=10:raise ValueError('リネン計画の承認済み対象は2026年10月です')
    parsed=parse_csv(csv_data,exceptions={})
    book=Xlsx(linen_data)
    try:
        sheets,dates=read_linen(book,start)
        snapshot=extract_snapshot(sheets,dates,start)
    finally:book.close()
    result=schedule_snapshot(snapshot,parsed,start,options)
    result['source_files']=[dict(kind='CSV',sha256=sha256(csv_data).hexdigest()),dict(kind='LINEN_XLSX',sha256=sha256(linen_data).hexdigest())]
    result['written_ranges']=[]
    result['external_sent']=False
    result['saved_to_server']=False
    return result


def extract_snapshot(sheets,dates,start):
    report_days=[(start+timedelta(days=i)).isoformat() for i in range(3)]
    # Orders within the report window need dispatch coverage up to their next receipt.
    end=min(date.fromisoformat(next_receipt(report_days[-1])),LIMIT)
    days=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    rooms={}
    stock,use,incoming,warehouse,ys=[sheets[n] for n in ('現在の在庫','部屋使用','部屋入庫','倉庫入出庫履歴','八代業者入庫履歴')]
    for room,m in MASTER['rooms'].items():
        c=dates['現在の在庫'].get(start.isoformat())
        if c is None:raise ValueError('業務日の朝在庫がありません')
        opening={i:amount(stock,column(c)+str(r),stock=True) for i,r in m['rows'].items()}
        room_days={}
        for day in days:
            if any(day not in dates[n] for n in ('部屋使用','部屋入庫')):raise ValueError('予測期間の日付見出しが不足しています')
            room_days[day]={kind:{i:amount(cells,column(dates[name][day])+str(r)) for i,r in m['rows'].items()}
                for kind,name,cells in [('usage','部屋使用',use),('incoming','部屋入庫',incoming)]}
        rooms[room]=dict(opening=opening,days=room_days)
    c=dates['現在の在庫'][start.isoformat()]
    warehouse_data=dict(opening={i:amount(stock,column(c)+str(246+n),stock=True) for n,i in enumerate(ITEMS)},days={})
    yorders={}
    for day in days:
        if day not in dates['倉庫入出庫履歴'] or day not in dates['八代業者入庫履歴']:
            raise ValueError('業者履歴の予測期間が不足しています')
        wc=column(dates['倉庫入出庫履歴'][day]);yc=column(dates['八代業者入庫履歴'][day])
        # Keep unfilled supplier cells unknown for state confirmation; forecast no
        # unrecorded receipt, while surfacing that omission in order calculations.
        warehouse_data['days'][day]={'receipts':{i:amount(warehouse,wc+str(4+n)) for n,i in enumerate(ITEMS)},
            'recorded':{i:value(warehouse,wc+str(4+n)) is not None for n,i in enumerate(ITEMS)}}
        yorders[day]={i:value(ys,yc+str(4+n)) for n,i in enumerate(ITEMS)}
    return dict(report_days=report_days,days=days,rooms=rooms,warehouse=warehouse_data,yatsushiro_orders=yorders)


def add(a,b):return None if a is None or b is None else a+b


def active_rows(parsed):
    return [r for r in parsed['reservations'] if not r['cancelled'] and r['room']]


def forecast_rooms(snapshot,extra=None):
    extra=extra or {};traces={};shortages=[];unknowns=[]
    for room,data in snapshot['rooms'].items():
        stock=dict(data['opening']);trace=[]
        for day in snapshot['days']:
            row=data['days'][day];before=dict(stock)
            for item in stock:
                stock[item]=add(stock[item],row['incoming'].get(item,0))
                stock[item]=add(stock[item],extra.get((room,day),{}).get(item,0))
                stock[item]=add(stock[item],None if row['usage'].get(item,0) is None else -row['usage'].get(item,0))
                if stock[item] is None:unknowns.append(dict(room=room,date=day,item=LABELS[item]))
                elif stock[item]<0:shortages.append(dict(room=room,date=day,item=LABELS[item],quantity=-stock[item]))
            trace.append(dict(date=day,before=before,after=dict(stock)))
        traces[room]=trace
    return traces,shortages,unknowns


def schedule_snapshot(snapshot,parsed,start,options=None):
    options=options or {};days=snapshot['days'];report_days=snapshot['report_days']
    if len(report_days)!=3 or report_days[0]!=start.isoformat():raise ValueError('3日間の開始日が一致しません')
    states=options.get('order_states',{})
    if not isinstance(states,dict) or any(v not in ('UNKNOWN','UNORDERED','ORDERED') for v in states.values()):raise ValueError('発注状態の指定が不正です')
    valid_order_days={d for d in report_days if d in days and date.fromisoformat(d).weekday() in (0,2,4)}
    if any(d not in valid_order_days for d in states):raise ValueError('発注状態は表示中の納品日だけ指定してください')
    all_issues=[i for i in parsed['issues'] if i['business']=='all']
    reservations=active_rows(parsed);timing=options.get('receipt_before_dispatch_confirmed') is True
    recorded={d:[] for d in report_days};entry_checks=[]
    for room,data in snapshot['rooms'].items():
        for day in report_days:
            if day not in data['days']:continue
            q=data['days'][day]['incoming']
            if any(v is None or v>0 for v in q.values()):
                check=entry_window(room,day,reservations)
                recorded[day].append(dict(room=room,quantities=q,region='八代' if room.startswith('八代:') else '熊本',
                                          status='EXCEL_RECORDED_UNCLASSIFIED',entry=check))
                entry_checks.append(dict(room=room,date=day,**check))
    traces,shortages,unknowns=forecast_rooms(snapshot)
    suggestions=[];proposal_notes=[];extras={};safe={}
    # Count-only PL exceptions do not enter this workflow. Use a separate explicit
    # confirmation for reservation/entry coverage before proposing additional moves.
    coverage=options.get('reservation_scope_confirmed') is True and not all_issues
    for room,data in snapshot['rooms'].items():
        if room.startswith(('八代:','出水:')):continue
        negative=[t for t in traces[room] if any(v is not None and v<0 for v in t['after'].values())]
        if not negative:continue
        first=negative[0]['date']
        if any(v is None or v<0 for v in data['opening'].values()):
            proposal_notes.append(dict(room=room,reason='朝在庫の実数確認が必要。補充で補正値の帳尻を合わせません'));continue
        if not coverage:
            proposal_notes.append(dict(room=room,reason='対象期間の予約・在室範囲未確認。既存予定だけを表示'));continue
        choices=[]
        for day in report_days:
            if day not in days or day>first:continue
            e=entry_window(room,day,reservations)
            if e['allowed']:choices.append((e['priority'],day,e))
        if not choices:
            proposal_notes.append(dict(room=room,reason='不足前の合法な入室機会が確認できません'));continue
        _,day,entry=min(choices,key=lambda x:(x[0],x[1]))
        t=next(t for t in traces[room] if t['date']==day);use=data['days'][day]['usage'];existing=data['days'][day]['incoming']
        _,target=profile(room);q={};held=[]
        for item,want in target.items():
            before=t['before'].get(item);u=use.get(item,0);inc=existing.get(item,0)
            if before is None or u is None or inc is None:q[item]=None;held.append(item)
            else:q[item]=max(0,want-before+u-inc)
        if held:
            proposal_notes.append(dict(room=room,reason='使用・既存入庫・在庫の未確認品目を含むため追加補充を保留'));continue
        if not any(q.values()):continue
        suggestion=dict(id=room+'@'+day,room=room,date=day,quantities=q,status='ADDITIONAL_DRAFT',entry=entry)
        suggestions.append(suggestion);extras[room,day]=q
    suggested_trace,suggested_shortages,suggested_unknowns=forecast_rooms(snapshot,extras)
    for suggestion in suggestions:
        eligible=[t for t in suggested_trace[suggestion['room']] if t['date']>=suggestion['date']]
        safe[suggestion['id']]={i:max(0,min(t['after'][i] for t in eligible)) if all(t['after'][i] is not None for t in eligible) else None for i in ('FACE','BATH')}
    reduction=towel_reduction(suggestions,safe) if suggestions else None
    warehouse=warehouse_forecast(snapshot,recorded,timing)
    orders=[]
    for day in sorted(valid_order_days):
        state=states.get(day,'UNKNOWN');until=next_receipt(day)
        required=[d for d in days if day<d<until]
        horizon_ok=date.fromisoformat(until)<=LIMIT and (date.fromisoformat(until)-timedelta(days=1)).isoformat() in days
        before=next(t['before'] for t in warehouse['trace'] if t['date']==day)
        dispatch=warehouse['dispatch'][day];source=snapshot['warehouse']['days'][day]
        order_items=[]
        for item in ITEMS:
            recorded_qty=source['receipts'].get(item) if source['recorded'].get(item) else None
            future_values=[warehouse['dispatch'][d][item] for d in required]
            future=None if not horizon_ok or any(v is None for v in future_values) else sum(future_values)
            fixed=recorded_qty if state=='ORDERED' else None
            if state=='ORDERED' and recorded_qty is None:
                candidate=dict(status='STOCK_UNKNOWN',quantity=None,candidates=[])
            elif before[item] is not None and before[item]<0:
                candidate=dict(status='STOCK_NEGATIVE',quantity=fixed,candidates=[])
            else:candidate=supplier_order(item,before[item],dispatch[item],future,fixed=fixed)
            # An unknown order's recorded quantity is never silently replaced.
            chosen=recorded_qty if state=='UNKNOWN' else candidate.get('quantity')
            order_items.append(dict(item=item,label=LABELS[item],recorded=recorded_qty,quantity=chosen,
                                    candidate=candidate.get('quantity'),status=candidate['status'],opening=before[item],
                                    dispatch=dispatch[item],until_next=future))
        can_copy=state!='UNKNOWN' and all(o['quantity'] is not None and o['status'] in ('DRAFT','ORDERED') for o in order_items)
        text=order_text(day,state,order_items,timing)
        orders.append(dict(date=day,state=state,next_receipt=until,items=order_items,copy_ready=can_copy,text=text,
                           basis='EXCEL_RECORDED_DELIVERIES_ONLY',note='追加補充案・タオル減量案は未確定のため、この発注計算には含めていません'))
    yatsushiro=[]
    for day in report_days:
        if day not in days:continue
        for item in ITEMS:
            entries=[data['days'][day]['incoming'].get(item,0) for room,data in snapshot['rooms'].items() if room.startswith('八代:')]
            direct=None if any(v is None for v in entries) else sum(entries)
            ordered=snapshot['yatsushiro_orders'].get(day,{}).get(item)
            if ordered not in (None,'') or direct:
                unallocated=None if ordered in (None,'') or direct is None else ordered-direct
                yatsushiro.append(dict(date=day,item=LABELS[item],recorded=ordered,direct=direct,unallocated=unallocated))
    next_unknown=[]
    for offset in range(0,len(days),3):
        batch=build_preparations(parsed,days[offset]);next_unknown.extend(i['message'] for i in batch['issues'] if i['code']=='STOCK_UNKNOWN')
    return dict(status='DRAFT_NOT_SENT',business_date=start.isoformat(),report_days=report_days,
        forecast_until=days[-1],recorded_by_date=recorded,additional_suggestions=suggestions,
        additional_notes=proposal_notes,towel_proposal=reduction,entry_checks=entry_checks,
        room_forecast=traces,shortages=shortages,unknowns=unknowns,
        after_additional_shortages=suggested_shortages,warehouse=warehouse,orders=orders,
        yatsushiro=yatsushiro,line_text=delivery_text(report_days,recorded,'熊本',days),
        yatsushiro_text=delivery_text(report_days,recorded,'八代',days),
        unavailable_dates=[d for d in report_days if d not in days],
        reservation_issues=all_issues,next_booking_unknowns=sorted(set(next_unknown)),
        limitations=['Excel記載の入庫・使用を前提とする予測。予定／実績・現場実数は未確定です',
            '発注案は既存の部屋入庫数量だけが対象。追加補充案と減量提案は別表示し、自動反映しません',
            'CSV未収録の滞在があり得ます。入室可否と納品→搬出→入庫→使用の実時刻を確認してください',
            '本番更新・外部送信・入力データのサーバー保存は行いません']+
            (['月末の承認済み予測期限を超える日付は計画・発注確定を保留します'] if any(d not in days for d in report_days) or any(not o['copy_ready'] and o['next_receipt']>LIMIT.isoformat() for o in orders) else []))


def warehouse_forecast(snapshot,recorded,timing):
    stock=dict(snapshot['warehouse']['opening']);trace=[];shortages=[];conditional=[];unknowns=[];dispatches={}
    for day in snapshot['days']:
        dispatch={i:0 for i in ITEMS}
        for room,data in snapshot['rooms'].items():
            if room.startswith('八代:'):continue
            for item,q in data['days'][day]['incoming'].items():dispatch[item]=add(dispatch[item],q)
        before=dict(stock);receipts=snapshot['warehouse']['days'][day]['receipts']
        for item in ITEMS:
            if before[item] is not None and dispatch[item] is not None and before[item]<dispatch[item] and (receipts.get(item) or 0)>0 and not timing:
                conditional.append(dict(date=day,item=LABELS[item],shortage_before_receipt=dispatch[item]-before[item]))
            stock[item]=add(stock[item],receipts.get(item));stock[item]=add(stock[item],None if dispatch[item] is None else -dispatch[item])
            if stock[item] is None:unknowns.append(dict(date=day,item=LABELS[item]))
            elif stock[item]<0:shortages.append(dict(date=day,item=LABELS[item],quantity=-stock[item]))
        dispatches[day]=dispatch;trace.append(dict(date=day,before=before,receipts=receipts,dispatch=dispatch,after=dict(stock)))
    return dict(trace=trace,dispatch=dispatches,shortages=shortages,unknowns=unknowns,timing_risks=conditional,timing_confirmed=timing)


def delivery_text(days,recorded,region,available_days=None):
    lines=[f'【{region}・Excel記載の搬入予定／実施状況未確認】']
    for day in days:
        lines.append('【'+day+'】')
        if available_days is not None and day not in available_days:
            lines.append('承認済み予測期限外・計画未作成');continue
        plans=[p for p in recorded[day] if p['region']==region]
        if not plans:lines.append('搬入記載なし（未確認の在庫・未配分は別途確認）')
        for plan in plans:
            lines.append(plan['room']+'：'+'／'.join(LABELS[i]+' '+('未確認' if q is None else str(q)) for i,q in plan['quantities'].items()))
            if not plan['entry']['allowed']:lines.append('※入室条件の確認待ち。未確認のまま実行しないでください')
    lines.append('※CO後・リネン使用前・CI前に搬入。担当者が実施状況と時刻を確認してください。')
    return '\n'.join(lines)


def order_text(day,state,items,timing):
    label={'UNKNOWN':'記載数量・発注状態未確認','UNORDERED':'未発注案','ORDERED':'発注済み数量保持'}[state]
    lines=[f'【熊本 {day}納品／{label}】']
    for item in items:lines.append(item['label']+'：'+('未確認' if item['quantity'] is None else str(item['quantity'])))
    if not timing:lines.append('※業者納品→倉庫搬出の順序・時刻を確認してください。')
    if any(i['status'] not in ('DRAFT','ORDERED') for i in items):lines.append('※不足・上限・未確認事項あり。数量を確定しないでください。')
    lines.append('※この画面から業者への送信はしていません。')
    return '\n'.join(lines)
