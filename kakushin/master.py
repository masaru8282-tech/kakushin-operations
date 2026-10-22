import json
import unicodedata
from pathlib import Path

MASTER = json.loads(Path(__file__).with_name('master.json').read_text())
RULE_VERSION = MASTER['version']
ITEMS = ('S_SHEET', 'S_DUVET', 'PILLOW', 'FACE', 'BATH', 'MAT',
         'Q_SHEET', 'Q_DUVET', 'SD_SHEET', 'SD_DUVET')
LABELS = dict(zip(ITEMS, ('Sシーツ', 'Sデュベ', 'ピロー', 'フェイス', 'バス', 'マット',
                         'Qシーツ', 'Qデュベ', 'SDシーツ', 'SDデュベ')))


def normalize(text):
    return unicodedata.normalize('NFKC', str(text)).strip()


ALIASES = {normalize(a): name for name, f in MASTER['facilities'].items()
           for a in f['aliases'] if a != 'jitekiso'}
ALIASES.update({name: name for name in MASTER['facilities']})
ALIASES.update({'水前寺2': '水前寺②', '水2': '水前寺②', 'セルジュ': '水前寺②'})
PL_MAP = {}
for col, label in MASTER['pl_headers'].items():
    if col in ('AF', 'AH', 'AI', 'AJ'):
        continue
    if col == 'AG':
        PL_MAP['二本木:'] = col
    else:
        facility, room = label.rsplit('_', 1)
        PL_MAP[f'{ALIASES.get(facility, facility)}:{room}'] = col
for room in ('101', '102', '103'):
    PL_MAP[f'八代:{room}'] = 'AI'
for room in ('104', '105', '106'):
    PL_MAP[f'八代:{room}'] = 'AJ'


def resolve(property_name, room):
    facility = ALIASES.get(normalize(property_name))
    key = f'{facility}:{normalize(room)}'
    return key if key in MASTER['rooms'] and facility != '出水' else None


def profile(room_key):
    if room_key not in MASTER['rooms'] or room_key.startswith('出水:'):
        raise ValueError('MAPPING_UNKNOWN: 通常使用数が未確定')
    room = MASTER['rooms'][room_key]
    keys = room['rows']
    use = {'PILLOW': 2, 'MAT': 1}
    target = {'PILLOW': 10, 'FACE': 10, 'BATH': 10, 'MAT': 5}
    for sheet, duvet in [('S_SHEET', 'S_DUVET'), ('Q_SHEET', 'Q_DUVET'), ('SD_SHEET', 'SD_DUVET')]:
        if sheet in keys:
            use[sheet] = use[duvet] = 1 if sheet == 'Q_SHEET' else 2
            target[sheet] = target[duvet] = 3 if sheet == 'Q_SHEET' else 6
    if 'SD_SHEET' in keys:
        use['PILLOW'] = 4
        target.update(PILLOW=16, FACE=20, BATH=20)
        if 'S_SHEET' in keys and room['facility'] != '二本木':
            use['SD_SHEET'] = use['SD_DUVET'] = 1
            target['SD_SHEET'] = target['SD_DUVET'] = 3
    if room['facility'] == '二本木':
        use['PILLOW'] = 6
    if room['facility'] == '八代':
        target = None
    return use, target
