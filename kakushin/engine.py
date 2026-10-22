from datetime import date, timedelta
import re

from .csvio import issue
from .master import MASTER, PL_MAP, profile, RULE_VERSION

PL_ROWS = {'sales': 6, 'count': 8, 'nights': 9, 'booking_fee': 24, 'airbnb_fee': 25, 'tax': 27}


def aggregate_pl(parsed, month, completeness):
    if not re.fullmatch(r'\d{4}-\d{2}', month):
        raise ValueError('対象月はYYYY-MM')
    date.fromisoformat(month + '-01')
    issues = [x for x in parsed['issues'] if x['business'] in ('all', 'pl')]
    if completeness != 'full':
        issues.append(issue('SCOPE_UNKNOWN', '全件宣言のないCSVで月全体を置換できません'))
    columns = {c: {k: (None if c in ('AI', 'AJ') and k == 'tax' else 0)
                   for k in PL_ROWS} for c in MASTER['pl_headers']}
    matched = 0
    for r in parsed['reservations']:
        if r.get('excluded'):
            continue
        accounting_month = r['exception_month'] if r['exception'] else (r['co'] or '')[:7]
        if accounting_month != month:
            continue
        matched += 1
        col = r['exception'] or PL_MAP.get(r['room'])
        if not col:
            issues.append(issue('MAPPING_UNKNOWN', 'PL列の正式対応が未確定', r['row'], business='pl'))
            continue
        target = columns[col]
        target['sales'] += r['sales']
        target['count'] += 1
        target['nights'] += 0 if r['exception'] else r['nights']
        fee_key = {'Booking.com': 'booking_fee', 'Airbnb': 'airbnb_fee'}.get(r['ota'])
        if fee_key:
            target[fee_key] += r['fee']
        if target['tax'] is not None and not r['exception']:
            target['tax'] += r['guests'] * r['nights'] * 200
    if matched == 0:
        issues.append(issue('SCOPE_UNKNOWN', '対象月の対象予約が0件。全月のゼロ置換は保留'))
    cells = {f'{col}{row}': values[key] for col, values in columns.items() for key, row in PL_ROWS.items()}
    totals = {key: sum(v[key] or 0 for v in columns.values()) for key in PL_ROWS}
    return dict(headers=MASTER['pl_headers'], month=month, status='HELD' if issues else 'SHADOW_VALIDATED', columns=columns,
                candidate_cells=cells, totals=totals, issues=issues, matched_rows=matched,
                writable=False)


def preparation(room, guests=None, nights=None, confirmed_no_next=False):
    use, _ = profile(room)
    use = dict(use)
    if confirmed_no_next:
        if guests is not None or nights is not None:
            raise ValueError('次回なし確認と予約入力は併用不可')
        guests, nights = 2, 1
    if guests is None or nights is None:
        use.update(FACE=None, BATH=None)
    else:
        if type(guests) is not int or type(nights) is not int or guests <= 0 or nights <= 0:
            raise ValueError('人数・泊数は正の整数')
        use.update(FACE=guests * min(nights, 3), BATH=guests * min(nights, 3))
    return use


def additional_preparation(required, prepared):
    quantities, unknowns = {}, []
    for item, needed in required.items():
        previous = prepared.get(item)
        if previous is None or needed is None:
            quantities[item] = None
            unknowns.append(item + ':準備済み数量未確認')
        else:
            quantities[item] = max(0, needed - previous)
            if previous > needed:
                unknowns.append(item + ':余剰の戻し要確認')
    return dict(quantities=quantities, unknowns=unknowns)


def build_preparations(parsed, business_date):
    start = date.fromisoformat(business_date)
    end = min(start + timedelta(days=2), date(2026, 10, 31))
    issues, events = [], []
    if start > end:
        return dict(status='HELD', events=[], issues=[issue('SCOPE_UNKNOWN', 'リネン予測期限2026/10/31を超えています')])
    if any(x['business'] == 'all' for x in parsed['issues']):
        return dict(status='HELD', events=[], issues=[issue('INPUT_INVALID', '予約入力に未解決の問題があり、清掃日の確定は保留')])
    active = [r for r in parsed['reservations'] if not r['cancelled'] and r['room']]
    for room in sorted({r['room'] for r in active}):
        reservations = sorted([r for r in active if r['room'] == room], key=lambda r: (r['ci'], r['co']))
        if any(a['co'] > b['ci'] for a, b in zip(reservations, reservations[1:])):
            issues.append(issue('REPORT_AMBIGUOUS', f'{room}:重複滞在があり準備を保留'))
            continue
        for index, current in enumerate(reservations):
            if not (business_date <= current['co'] <= end.isoformat()):
                continue
            following = reservations[index + 1] if index + 1 < len(reservations) else None
            use = preparation(room, following['guests'] if following else None,
                              following['nights'] if following else None)
            events.append(dict(event_id=f'{room}:{current["co"]}:cleaning', room=room,
                               service_date=current['co'], quantities=use, status='DRAFT',
                               next_reservation_row=following['row'] if following else None,
                               sequence='CO → 搬入 → 使用・準備 → 次回CI',
                               source_row=current['row']))
            if not following:
                issues.append(issue('STOCK_UNKNOWN', f'{room}:次回予約未確認、タオルのみ保留', current['row'], business='linen'))
    if end < start + timedelta(days=2):
        issues.append(issue('SCOPE_UNKNOWN', '3日間の一部が10/31を超えるため期限内だけ計算', business='linen'))
    return dict(status='PARTIAL' if issues else 'SHADOW_VALIDATED', events=events, issues=issues)


def process(parsed, month, completeness, business_date):
    return dict(mode='SHADOW', business_date=business_date, rule_versions=[RULE_VERSION],
                source_files=[dict(sha256=parsed['sha256'], schema=parsed['schema'],
                                   row_count=len(parsed['reservations']), completeness=completeness)],
                PL=aggregate_pl(parsed, month, completeness), linen=build_preparations(parsed, business_date),
                warnings=['在庫スナップショット未入力のため不足判定・搬入計画・発注は未実施',
                          '実績・既存準備済み数との照合前の使用候補です。シート反映はしていません'],
                written_ranges=[], verification={'external_sheets': 'NOT_RUN', 'external_slack': 'NOT_RUN'})
