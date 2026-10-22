"""Validate immutable offline sheet snapshots and preview bounded writes.

No live apply is exposed: Sheets has no compare-and-swap and production ownership,
concurrency and operator roles remain unresolved (P02/P06/D02).
"""
import re

from .master import MASTER
from .engine import PL_ROWS


class ProjectionError(ValueError):
    pass


def validate_pl_snapshot(snapshot, month):
    expected = {'2026-10': ('OCT2026', 50055757, '=SUM(B27:AG27)'),
                '2026-11': ('NOV2026', 54662479, '=SUM(B27:AH27)')}.get(month)
    if not expected:
        raise ProjectionError('SHEET_SCHEMA_CHANGED: 月別構造が未承認')
    tab, sheet_id, tax_formula = expected
    if (snapshot.get('tab'), snapshot.get('sheet_id')) != (tab, sheet_id):
        raise ProjectionError('SHEET_SCHEMA_CHANGED: タブ名/ID不一致')
    cells = snapshot['cells']
    for col, header in MASTER['pl_headers'].items():
        if cells.get(col + '5', {}).get('value') != header:
            raise ProjectionError(f'SHEET_SCHEMA_CHANGED: {col}5 の見出し不一致')
    for row in PL_ROWS.values():
        expected_formula = tax_formula if row == 27 else f'=SUM(B{row}:AJ{row})'
        formula = cells.get(f'AK{row}', {}).get('formula', '')
        if re.sub(r'\s+', '', formula).upper() != expected_formula:
            raise ProjectionError(f'SHEET_SCHEMA_CHANGED: AK{row}合計式不一致')


def preview_pl(snapshot, result, month):
    validate_pl_snapshot(snapshot, month)
    if result.get('month') != month:
        raise ProjectionError('SCOPE_UNKNOWN: 集計月と投影月が不一致')
    if result['status'] != 'SHADOW_VALIDATED':
        raise ProjectionError('未解決のPLを投影できません')
    changes = []
    for address, after in result['candidate_cells'].items():
        cell = snapshot['cells'].get(address)
        if cell is None:
            raise ProjectionError(f'SHEET_SCHEMA_CHANGED: {address}未取得')
        if cell.get('formula') or cell.get('protected') or cell.get('validation'):
            raise ProjectionError(f'SHEET_SCHEMA_CHANGED: {address}数式/保護/制約セル')
        if cell.get('ownership') != 'SYSTEM_PL_INPUT':
            raise ProjectionError(f'CONCURRENT_EDIT: {address}の所有権未確認')
        if cell.get('value') != after:
            changes.append(dict(cell=address, before=cell.get('value'), after=after))
    return dict(mode='SHADOW', changes=changes, applied=False,
                warning='本番書込は未提供。比較と目視確認用の差分です')


def verify_readback(changes, current):
    if all(current.get(c['cell']) == c['after'] for c in changes):
        return 'APPLIED'
    if all(current.get(c['cell']) == c['before'] for c in changes):
        return 'NOT_APPLIED'
    return 'WRITE_UNCERTAIN'
