import base64
from datetime import datetime
from zoneinfo import ZoneInfo

from .planning import draft_plan, towel_reduction
from .csvio import parse_csv
from .engine import process
from .linen import parse_stock_sequence, supplier_order, reconcile, target_delivery, simulate_stock, yatsushiro_supply


def run_csv(store, data, month, completeness, business_date, private_exceptions=True):
    return store.execute(data, month, completeness, business_date,
                         lambda: process(parse_csv(data, exceptions=None if private_exceptions else {}), month, completeness, business_date))


def stock_tool(payload):
    operation = payload['operation']
    args = payload['args']
    functions = {'draft_plan': draft_plan, 'towel_reduction': towel_reduction, 'parse_report': parse_stock_sequence, 'supplier_order': supplier_order,
                 'reconcile': reconcile, 'target_delivery': target_delivery,
                 'simulate_stock': simulate_stock, 'yatsushiro_supply': yatsushiro_supply}
    if operation not in functions:
        raise ValueError('未対応の計算')
    return functions[operation](**args)


def today():
    return datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()
