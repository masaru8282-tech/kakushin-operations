"""Explainable draft planner. P08 tie breaks are a proposal, never a published rule."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .linen import quantity, entry_window, target_delivery, simulate_stock, next_receipt
from .master import MASTER, ITEMS, LABELS, profile

POLICY = 'proposal-v1: priority then earliest; towel half-up, largest safe slack first'


def towel_reduction(plans, safe_reduction):
    """A separately displayed P08 proposal; safe slack must cover the full horizon."""
    result = deepcopy(plans)
    report = {}
    for item in ('FACE', 'BATH'):
        if any(p['quantities'].get(item) is None for p in result):
            report[item] = dict(status='STOCK_UNKNOWN')
            continue
        total = sum(p['quantities'].get(item, 0) for p in result)
        wanted = int((Decimal(total) / 10).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        left = wanted
        ranked = sorted(result, key=lambda p: (-safe_reduction.get(p['id'], {}).get(item, 0), p['date'], p['room']))
        exclusions = []
        for p in ranked:
            slack = safe_reduction.get(p['id'], {}).get(item)
            quantity(slack)
            if slack is None or slack == 0:
                exclusions.append(dict(room=p['room'], reason='次回納品までの余裕なし・未確認'))
                continue
            amount = min(slack, p['quantities'].get(item, 0), left)
            p['quantities'][item] = p['quantities'].get(item, 0) - amount
            left -= amount
        reduced = wanted - left
        report[item] = dict(normal=total, final=total-reduced, reduced=reduced,
                            rate=round(100*reduced/total, 2) if total else 0,
                            exclusions=exclusions,
                            reason='不足を避けるため削減可能数まで' if left else '提案の10%丸め値まで削減')
    return dict(status='PROPOSAL_REQUIRES_POLICY_APPROVAL', policy=POLICY,
                plans=result, reduction=report)


def draft_plan(business_date, forecast_until, room_stock, usage_events, reservations,
               reservation_scope_confirmed=False, permissions=None, warehouse=None):
    """Inputs are an explicitly supplied snapshot, not silently read from legacy cells.

    Plans cover three days; stock validation reaches the next supplier receipt.
    Warehouse receipts require caller-provided sequence evidence.
    """
    start, end = date.fromisoformat(business_date), date.fromisoformat(forecast_until)
    next_day = date.fromisoformat(next_receipt(business_date))
    if end < max(next_day, start+timedelta(days=2)) or end > date(2026,10,31) or start > end:
        raise ValueError('次回納品日まで、かつ承認期限2026/10/31以内の予測範囲が必要')
    days=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    report_days=days[:3]
    permissions = set(permissions or [])
    unknowns, shortages, plans, evidence = [], [], [], []
    usage={}
    for event in usage_events:
        room, day = event['room'], event['date']
        if room not in MASTER['rooms'] or day not in days:
            raise ValueError('使用イベントの部屋・対象日が範囲外')
        key=(room,day)
        if key in usage:
            raise ValueError('同じ部屋・日の使用は事前に統合してください')
        if set(event['quantities']) - set(MASTER['rooms'][room]['rows']):
            raise ValueError('部屋で使用しない品目')
        for q in event['quantities'].values(): quantity(q)
        usage[key]=event['quantities']
    traces={}
    safe={}
    for room in sorted({key[0] for key in usage}):
        if room.startswith('八代:') or room.startswith('出水:'):
            unknowns.append(f'{room}:別運用・未確定のため熊本計画から分離')
            continue
        _, targets=profile(room)
        stock={i:room_stock.get(room,{}).get(i) for i in targets}
        for q in stock.values(): quantity(q)
        if any(v is None for v in stock.values()) or any(v is None for (r,_),u in usage.items() if r==room for v in u.values()):
            unknowns.append(f'{room}:在庫または使用の未確認品目があり自動割当保留')
            continue
        # Only replenish a room if its forecast would become negative.
        running=dict(stock)
        first_shortage=None
        for day in days:
            for item,q in usage.get((room,day),{}).items():
                running[item]-=q
            if min(running.values())<0 and first_shortage is None:
                first_shortage=day
        selected=None
        if first_shortage:
            if reservation_scope_confirmed is True:
                choices=[]
                for day in report_days:
                    if day>first_shortage: continue
                    e=entry_window(room,day,reservations,permission=f'{room}@{day}' in permissions)
                    if e['allowed']: choices.append((e['priority'],day,e))
                if choices:
                    _,selected,why=min(choices,key=lambda x:(x[0],x[1]))
                    evidence.append(dict(room=room,date=selected,entry=why))
            if selected is None:
                shortages.append(dict(room=room,date=first_shortage,reason='不足前の確認済み搬入機会なし'))
        running=dict(stock)
        trace=[]
        created=None
        for day in days:
            use=usage.get((room,day),{})
            if day==selected:
                q=target_delivery(room,running,use)['quantities']
                created=dict(id=f'{room}@{day}',room=room,date=day,quantities=q,status='CANDIDATE',
                             sequence='CO → 搬入 → 使用 → CI（時刻確認前）')
                plans.append(created)
                for item,amount in q.items():running[item]+=amount
            for item,amount in use.items():running[item]-=amount
            trace.append(dict(date=day,stock=dict(running)))
            for item,value in running.items():
                if value<0:shortages.append(dict(room=room,date=day,item=item,shortage=-value))
        traces[room]=trace
        if created:
            relevant=[t for t in trace if t['date']>=selected]
            safe[created['id']]={i:max(0,min(t['stock'][i] for t in relevant)) for i in ('FACE','BATH')}
            created['target_gap_at_end']={i:running[i]-target for i,target in targets.items()}
    proposal=towel_reduction(plans,safe)
    normal_dispatch={day:{i:0 for i in ITEMS} for day in report_days}
    for p in plans:
        for item,q in p['quantities'].items(): normal_dispatch[p['date']][item]+=q
    warehouse_result=None
    if warehouse is not None:
        # Caller must supply timed actual/fixed receipt events. Dispatch order is explicit too.
        events=deepcopy(warehouse['events'])
        times=warehouse.get('dispatch_sequence',{})
        for day,quantities in normal_dispatch.items():
            if any(quantities.values()):
                if day not in times:
                    raise ValueError('倉庫搬出時点のsequenceを指定してください')
                events.append(dict(id='planned-dispatch:'+day,date=day,sequence=times[day],kind='DISPATCH',quantities=quantities))
        events.sort(key=lambda e:(e['date'],e['sequence']))
        warehouse_result=simulate_stock(warehouse['opening'],events)
    else:
        unknowns.append('倉庫の実数・時点別納品情報なし。搬出可否と業者発注は未検証')
    return dict(status='CANDIDATE_NOT_CONFIRMED', policy=POLICY, forecast_until=forecast_until,
                plans_by_date={d:[p for p in plans if p['date']==d] for d in report_days},
                normal_dispatch=normal_dispatch, towel_proposal=proposal,
                room_forecast=traces, entry_evidence=evidence, warehouse=warehouse_result,
                shortages=shortages, unknowns=unknowns,
                warnings=['P08提案規則を使用。目標超え不足の再配分・施設まとめの最適化は未実装',
                          '実時刻・予約範囲・既存実績との照合前。予定確定や発注はしていません'],
                line_text=line_text(report_days,plans))


def line_text(days,plans):
    lines=[]
    for day in days:
        lines.append(f'【{day} リネン搬入・未確定案】')
        matches=[p for p in plans if p['date']==day and any(p['quantities'].values())]
        if not matches: lines.append('搬入案なし（不足・保留事項は別途確認）')
        for p in matches:
            lines.append(p['room']+'：'+ '／'.join(f'{LABELS[i]} {q}' for i,q in p['quantities'].items()))
        lines.append('※CO後・使用前・CI前の順序および時刻を確認')
    return '\n'.join(lines)
