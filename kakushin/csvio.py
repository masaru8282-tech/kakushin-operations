import csv
import hashlib
import io
import re
import json
import os
from pathlib import Path
from collections import Counter
from datetime import datetime

from .master import resolve, normalize

REQUIRED = {'予約サイト', 'チェックイン', 'チェックアウト', '物件名', '部屋番号',
            'ゲスト数', '合計日数', '通貨', '販売', 'OTA サービス料'}
# These exceptions are scoped to October 2026 and verified against identity AND context.
def load_exceptions():
    """Private approved reservation exceptions; deliberately excluded from Git."""
    path = Path(os.environ.get('KAKUSHIN_EXCEPTIONS_FILE', '.runtime/private-exceptions.json'))
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError('個別特例設定はオブジェクトが必要')
    return data



def issue(code, message, row=None, severity='error', business='all'):
    return dict(code=code, message=message, row=row, severity=severity, business=business)


def parse_date(value):
    if not value:
        return None
    if not re.fullmatch(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', value):
        raise ValueError('日付形式が不正')
    return datetime.strptime(value.replace('/', '-'), '%Y-%m-%d').date().isoformat()


def integer(value, name):
    # CSV quoting handles commas; currency symbols/decimal rounding are not inferred.
    if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)', value):
        raise ValueError(f'{name}は0以上の整数が必要')
    return int(value.replace(',', ''))


def parse_csv(data):
    exceptions = load_exceptions()
    result = dict(sha256=hashlib.sha256(data).hexdigest(), schema=None, reservations=[], issues=[])
    try:
        text = data.decode('utf-8-sig', errors='strict')
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        headers = next(reader)
        if len(headers) != len(set(headers)) or not REQUIRED <= set(headers):
            raise ValueError('必須列不足または列名重複')
        size = len(headers)
        if size not in (13, 14, 15, 44):
            raise ValueError('未確認CSVスキーマ（13/14/15/44列のみ対応）')
        ids = {'AirHost予約ID', 'チャンネル予約ID'} <= set(headers)
        if (size in (15, 44) and not ids) or (size == 14 and 'キャンセル' not in headers) or (size == 44 and not {'状態', 'キャンセル', '更新日時'} <= set(headers)):
            raise ValueError('列数とスキーマ識別列が一致しない')
        result['schema'] = {13: 'A', 14: 'C', 15: 'B', 44: 'D'}[size]
        seen = Counter()
        anonymous = Counter()
        for row_no, values in enumerate(reader, 2):
            if not values:
                continue
            if len(values) != size:
                result['issues'].append(issue('INPUT_INVALID', '列数がヘッダーと不一致', row_no))
                continue
            raw = dict(zip(headers, values))
            r = {k: normalize(v) for k, v in raw.items()}
            try:
                row = dict(row=row_no, airhost_id=r.get('AirHost予約ID', ''),
                           channel_id=r.get('チャンネル予約ID', ''), ota=r['予約サイト'],
                           property=r['物件名'], room=resolve(r['物件名'], r['部屋番号']),
                           room_number=r['部屋番号'], ci=parse_date(r['チェックイン']),
                           co=parse_date(r['チェックアウト']),
                           guests=integer(r['ゲスト数'], '人数'), nights=integer(r['合計日数'], '泊数'),
                           currency=r['通貨'], sales=integer(r['販売'], '販売'),
                           fee=integer(r['OTA サービス料'], '手数料'),
                           status=r.get('状態', ''), cancelled=bool(r.get('キャンセル', '')),
                           exception=None)
                if row['status'] not in ('', '確認済み', 'キャンセル', 'システムキャンセル'):
                    raise ValueError('未確認の予約状態')
                row['cancelled'] |= row['status'] in ('キャンセル', 'システムキャンセル')
                if r.get('キャンセル'):
                    parse_date(r['キャンセル'])
                if row['currency'] != 'JPY':
                    raise ValueError('JPY以外の通貨は未対応')
                ex = exceptions.get(row['airhost_id'])
                if ex:
                    if not (row['ota'] == ex['ota'] and row['sales'] == ex['sales'] and row['fee'] == ex['fee']
                            and (row['ci'], row['co']) in ((None, None), (ex['ci'], ex['co']))
                            and (row['room'] == ex['room'] if ex['room'] else
                                 row['property'] == '08_vetta@yatsushiro Egretta' and row['room_number'] == '')
                            and row['nights'] in (0, (datetime.fromisoformat(ex['co']) - datetime.fromisoformat(ex['ci'])).days)
                            and (row['ci'] is not None or row['nights'] == 0)
                            and ('guests' not in ex or row['guests'] == ex['guests'])):
                        raise ValueError('個別特例のIDと施設・日付・金額等が不一致')
                    row.update(exception=ex['column'], exception_month=ex['month'], cancelled=True)
                elif row['cancelled']:
                    if row['sales'] or row['fee']:
                        result['issues'].append(issue('INPUT_INVALID', '新しい取消料は承認が必要', row_no, business='pl'))
                    row['excluded'] = True
                else:
                    if not row['ci'] or not row['co'] or row['guests'] <= 0 or row['nights'] <= 0:
                        raise ValueError('通常予約の有効日付・正の人数/泊数が必要')
                    diff = (datetime.fromisoformat(row['co']) - datetime.fromisoformat(row['ci'])).days
                    if diff != row['nights']:
                        raise ValueError('日付差と合計日数が不一致')
                if not row['room'] and not row['exception'] and not row['cancelled']:
                    result['issues'].append(issue('MAPPING_UNKNOWN', '施設＋部屋の対応が未確定', row_no))
                if row['ota'] not in ('Airbnb', 'Booking.com') and not row['cancelled']:
                    result['issues'].append(issue('INPUT_INVALID', '未承認OTAの手数料処理', row_no, business='pl'))
                identity = row['airhost_id'] or (row['ota'] + ':' + row['channel_id'] if row['channel_id'] else '')
                if identity:
                    seen[identity] += 1
                    if seen[identity] > 1:
                        result['issues'].append(issue('INPUT_INVALID', '同一予約IDが重複', row_no))
                else:
                    fingerprint = tuple(row[k] for k in ('property', 'room_number', 'ci', 'co', 'ota'))
                    anonymous[fingerprint] += 1
                    if anonymous[fingerprint] > 1:
                        result['issues'].append(issue('REPORT_AMBIGUOUS', 'IDなし重複候補。行は削除していません', row_no))
                result['reservations'].append(row)
            except ValueError as e:
                result['issues'].append(issue('INPUT_INVALID', str(e), row_no))
        if not result['reservations']:
            result['issues'].append(issue('INPUT_INVALID', '有効な予約行がありません'))
    except (UnicodeDecodeError, csv.Error, ValueError, StopIteration) as e:
        result['issues'].append(issue('INPUT_INVALID', f'CSV読取不可: {e}'))
    return result
