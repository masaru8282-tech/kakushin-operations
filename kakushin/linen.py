"""Pure stock/report/planning primitives. No network or sheet mutations."""
from datetime import date, timedelta
import re

from .master import ITEMS, MASTER, profile, LABELS

ORDER_RULES = dict(zip(ITEMS, [(30, 10, 50), (30, 5, 50), (50, 50, 100),
    (60, 20, 120), (60, 10, 120), (20, 20, 40), (10, 10, None),
    (10, 5, None), (10, 10, None), (10, 5, None)]))


def quantity(value, nullable=True):
    if value is None and nullable:
        return value
    if type(value) is not int or value < 0:
        raise ValueError('数量は0以上の整数、未確認はnull')
    return value


def parse_stock_sequence(room, text):
    """Parse only explicit room-context sequences; do not infer room/date/phase."""
    if room not in MASTER['rooms']:
        raise ValueError('MAPPING_UNKNOWN')
    rows = MASTER['rooms'][room]['rows']
    order = list(rows)
    # Reports use S/D/P/F/B/M then explicitly sized SD pairs; sheets may differ.
    base = [i for i in ITEMS[:6] if i in rows]
    if 'Q_SHEET' in rows:
        base = ['Q_SHEET', 'Q_DUVET', 'PILLOW', 'FACE', 'BATH', 'MAT']
    elif 'S_SHEET' not in rows and 'SD_SHEET' in rows:
        base = ['SD_SHEET', 'SD_DUVET', 'PILLOW', 'FACE', 'BATH', 'MAT']
    tokens = text.strip().split('/')
    values = {i: None for i in rows}
    extras = {'Q': 0, 'SD': 0}
    sized = {'Q': ['Q_SHEET', 'Q_DUVET'], 'SD': ['SD_SHEET', 'SD_DUVET']}
    for index, raw in enumerate(tokens):
        token = raw.strip().upper()
        if not token:
            continue
        match = re.fullmatch(r'(SD|Q)?(\d+)', token)
        if not match:
            raise ValueError('REPORT_AMBIGUOUS: 品目数列を解析できません')
        size, number = match.groups()
        if size:
            if extras[size] >= 2:
                raise ValueError('REPORT_AMBIGUOUS: 同サイズの品目が過剰')
            item = sized[size][extras[size]]
            extras[size] += 1
        elif index < len(base):
            item = base[index]
        else:
            raise ValueError('REPORT_AMBIGUOUS: 混在サイズはSD/Qを明記してください')
        if item not in rows or values[item] is not None:
            raise ValueError('REPORT_AMBIGUOUS: 部屋にない品目、または重複指定')
        values[item] = int(number)
    if all(v is None for v in values.values()):
        raise ValueError('REPORT_AMBIGUOUS: 報告値なし')
    return {'quantities': values, 'sheet_order': [values[i] for i in order]}


def reconcile(initial, calculated, reported):
    if type(initial) is not int:
        raise ValueError('初期補正基準は整数が必要')
    quantity(reported)
    if reported is None or calculated is None:
        return dict(delta=None, after=initial, status='STOCK_UNKNOWN')
    if type(calculated) is not int:
        raise ValueError('計算在庫は整数が必要')
    delta = reported - calculated
    return dict(delta=delta, after=initial + delta, status='CANDIDATE')


def next_receipt(day, include_today=False):
    d = date.fromisoformat(day) + timedelta(days=0 if include_today else 1)
    while d.weekday() not in (0, 2, 4):
        d += timedelta(days=1)
    return d.isoformat()


def supplier_order(item, opening, dispatch_today, dispatch_until_next, fixed=None):
    if item not in ORDER_RULES:
        raise ValueError('未知品目')
    for value in (opening, dispatch_today, dispatch_until_next, fixed):
        if value is not None and type(value) is not int:
            raise ValueError('在庫・搬出・固定発注数は整数')
    if any(x is None for x in (opening, dispatch_today, dispatch_until_next)):
        return dict(status='STOCK_UNKNOWN', quantity=fixed, candidates=[])
    quantity(dispatch_today, False)
    quantity(dispatch_until_next, False)
    cap, unit, limit = ORDER_RULES[item]
    upper = min(limit, max(0, cap - opening + dispatch_today)) if limit is not None else max(0, cap - opening + dispatch_today)
    candidates = [q for q in range(0, upper + 1, unit)
                  if 0 <= opening + q - dispatch_today <= cap
                  and opening + q - dispatch_today >= dispatch_until_next]
    if fixed is not None:
        quantity(fixed, False)
        end = opening + fixed - dispatch_today
        return dict(status='ORDERED' if fixed in candidates else 'FIXED_ORDER_CONFLICT',
                    quantity=fixed, end_stock=end, candidates=candidates,
                    note='発注済み数量を保持。日中の時点別在庫は別途検証')
    if not candidates:
        return dict(status='ORDER_INFEASIBLE', quantity=None, candidates=[],
                    existing_over_capacity=max(0, opening - dispatch_today - cap))
    q = max(candidates)
    return dict(status='DRAFT', quantity=q, end_stock=opening + q - dispatch_today,
                candidates=candidates, note='日中の納品・搬出順序の確認が必要')


def entry_window(room, day, reservations, permission=False):
    date.fromisoformat(day)
    if room not in MASTER['rooms']:
        raise ValueError('MAPPING_UNKNOWN')
    stays = [r for r in reservations if r['room'] == room and not r['cancelled']]
    if any(r['ci'] < day < r['co'] for r in stays):
        return dict(allowed=bool(permission), priority=1 if permission else None,
                    reason='個別入室許可' if permission else 'ENTRY_FORBIDDEN:宿泊中')
    own = any(day in (r['ci'], r['co']) for r in stays)
    other = any(r['room'] and r['room'].split(':')[0] == room.split(':')[0]
                and r['room'] != room and r['co'] == day and not r['cancelled'] for r in reservations)
    if own or permission or other:
        return dict(allowed=True, priority=1 if own or permission else 2,
                    reason='CO後・使用前・CI前の順序確認が必要', conditional=not permission)
    return dict(allowed=False, priority=None, reason='承認済み搬入機会なし')


def target_delivery(room, stock, usage):
    _, target = profile(room)
    if target is None:
        return dict(status='HELD', reason='補充目標未確定', quantities={})
    q = {}
    for item, desired in target.items():
        before, used = stock.get(item), usage.get(item, 0)
        quantity(before)
        quantity(used)
        q[item] = None if before is None or used is None else max(0, desired - before + used)
    return dict(status='PARTIAL' if None in q.values() else 'CANDIDATE', quantities=q,
                towel_reduction='PENDING_POLICY_P08', target=target)


def simulate_stock(opening, events):
    """Events are supplied in explicit physical sequence, not sorted by date alone."""
    stock = {i: opening.get(i) for i in ITEMS}
    for value in stock.values():
        quantity(value)
    shortages, unknowns, trace = [], [], []
    previous = None
    seen = set()
    for event in events:
        stamp = (event['date'], event['sequence'])
        date.fromisoformat(event['date'])
        if type(event['sequence']) is not int or previous is not None and stamp <= previous:
            raise ValueError('イベントの時点順序は一意かつ昇順で指定')
        previous = stamp
        if event['id'] in seen:
            raise ValueError('イベントID重複')
        seen.add(event['id'])
        if event['kind'] not in ('RECEIPT', 'DISPATCH', 'USE', 'TRANSFER_IN', 'TRANSFER_OUT'):
            raise ValueError('未知の在庫イベント')
        sign = 1 if event['kind'] in ('RECEIPT', 'TRANSFER_IN') else -1
        for item, q in event['quantities'].items():
            if item not in ITEMS:
                raise ValueError('未知品目')
            quantity(q)
            if q is None or stock[item] is None:
                stock[item] = None
                unknowns.append(dict(event=event['id'], item=item))
            else:
                stock[item] += sign * q
                if stock[item] < 0:
                    shortages.append(dict(event=event['id'], date=event['date'], item=item, shortage=-stock[item]))
        trace.append(dict(event=event['id'], stock=dict(stock)))
    return dict(stock=stock, shortages=shortages, unknowns=unknowns, trace=trace)


def yatsushiro_supply(destination, need, own_stock, donors, destination_entry_confirmed=False):
    if not destination.startswith('八代:') or destination not in MASTER['rooms']:
        raise ValueError('八代の既知部屋が必要')
    if destination_entry_confirmed is not True:
        return dict(transfers=[], minimum_supplier_need=None, unknowns=['受入室の当日入室が未確認'],
                    status='HELD', warning='ENTRY_FORBIDDEN: 受入室の当日入室確認が必要')
    remaining, transfers, held = {}, [], []
    for item, q in need.items():
        if item not in MASTER['rooms'][destination]['rows']:
            raise ValueError('部屋で使用しない品目')
        quantity(q, False)
        available = own_stock.get(item)
        quantity(available)
        if available is None:
            held.append(item)
            remaining[item] = None
        else:
            remaining[item] = max(0, q - available)
    seen = {destination}
    for donor in sorted(donors, key=lambda d: d['room']):
        source = donor['room']
        if source in seen or source not in MASTER['rooms'] or not source.startswith('八代:'):
            raise ValueError('移動元は重複のない別の八代部屋')
        seen.add(source)
        if donor.get('entry_confirmed') is not True:
            continue
        for item, missing in remaining.items():
            if not missing or item not in MASTER['rooms'][source]['rows']:
                continue
            actual, reserve = donor['stock'].get(item), donor['reserved'].get(item)
            quantity(actual)
            quantity(reserve)
            if actual is None or reserve is None:
                continue
            q = min(missing, max(0, actual - reserve))
            if q:
                transfers.append(dict(source=source, destination=destination, item=item, quantity=q, status='DRAFT'))
                remaining[item] -= q
    return dict(transfers=transfers, minimum_supplier_need=remaining, unknowns=held,
                status='HELD' if held else 'CANDIDATE',
                warning='受入室の入室確認・業者条件・移動台帳の承認前。熊本倉庫には計上しません')


def transfer_balances(source, destination, item, q, source_access, destination_access):
    quantity(q, False)
    if item not in ITEMS or not source_access or not destination_access:
        raise ValueError('ENTRY_FORBIDDEN')
    if source.get(item) is None or destination.get(item) is None or source[item] < q:
        raise ValueError('STOCK_UNKNOWN または STOCK_SHORTAGE')
    a, b = dict(source), dict(destination)
    a[item] -= q
    b[item] += q
    return a, b


def supplier_text(day, quantities, region='熊本', ordered=False):
    date.fromisoformat(day)
    lines = [f'【{region} {day} 納品 {"発注済み" if ordered else "未発注案"}】']
    for item in ITEMS:
        q = quantities.get(item)
        quantity(q)
        lines.append(f'{LABELS[item]}：{q if q is not None else "未確認"}')
    return '\n'.join(lines)
