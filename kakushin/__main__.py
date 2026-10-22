import argparse
import json
from pathlib import Path

from .service import run_csv, stock_tool, today
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='KAKUSHIN 受付業務（シャドーモード）')
    parser.add_argument('--state-dir', default='.runtime')
    sub = parser.add_subparsers(dest='command', required=True)
    web = sub.add_parser('serve')
    web.add_argument('--port', type=int, default=8000)
    run = sub.add_parser('run')
    run.add_argument('csv', type=Path)
    run.add_argument('--month', required=True)
    run.add_argument('--business-date', default=today())
    run.add_argument('--completeness', choices=('full', 'partial', 'unknown'), default='unknown')
    tool = sub.add_parser('tool')
    tool.add_argument('input', type=Path, help='operationとargsを含むJSON')
    backup = sub.add_parser('backup')
    backup.add_argument('destination', type=Path)
    args = parser.parse_args()
    if args.command == 'tool':
        print(json.dumps(stock_tool(json.loads(args.input.read_text())), ensure_ascii=False, indent=2))
        return
    store = Store(args.state_dir)
    if args.command == 'serve':
        from .web import serve
        serve(store, args.port)
    elif args.command == 'backup':
        if args.destination.exists():
            parser.error('既存のバックアップは上書きしません')
        store.backup(args.destination)
        print('DB backup complete; 原本CSVアーカイブは別途保全してください')
    else:
        try:
            result, reused = run_csv(store, args.csv.read_bytes(), args.month, args.completeness, args.business_date)
        except ValueError as e:
            parser.error(str(e))
        print(json.dumps(dict(result=result, reused=reused), ensure_ascii=False, indent=2))
        if result['PL']['status'] == 'HELD':
            raise SystemExit(2)


if __name__ == '__main__':
    main()
