import argparse
import json
import tempfile
import os
from pathlib import Path

from .service import run_csv, stock_tool, today
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='KAKUSHIN 受付業務（シャドーモード）')
    parser.add_argument('--state-dir', default='.runtime')
    sub = parser.add_subparsers(dest='command', required=True)
    web = sub.add_parser('serve')
    web.add_argument('--port', type=int, default=8000)
    web.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'), default='127.0.0.1')
    web.add_argument('--review', action='store_true', help='読み取り専用ファイル照合・入力非保存')
    web.add_argument('--allowed-host', help='クラウド照合を公開する正確なホスト名')
    web.add_argument('--demo', action='store_true', help='架空データ専用・一時DB・実CSV受付なし')
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
    if args.command == 'serve' and args.demo and args.review:
        parser.error('--demo と --review は併用できません')
    if args.command == 'serve' and args.review:
        from .web import serve
        allowed = args.allowed_host
        if not allowed and os.environ.get('CODESPACE_NAME'):
            allowed = os.environ['CODESPACE_NAME'] + '-' + str(args.port) + '.' + os.environ.get('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN', 'app.github.dev')
        if args.host != '127.0.0.1' and not allowed:
            parser.error('クラウド照合にはCodespacesまたは --allowed-host の設定が必要です')
        with tempfile.TemporaryDirectory(prefix='kakushin-review-') as temporary:
            serve(Store(temporary), args.port, host=args.host, review=True, allowed_host=allowed)
        return
    if args.command == 'serve' and args.demo:
        from .web import serve
        with tempfile.TemporaryDirectory(prefix='kakushin-demo-') as temporary:
            serve(Store(temporary), args.port, host=args.host, demo=True)
        return
    if args.command == 'serve' and args.host != '127.0.0.1':
        parser.error('外部からの接続は --demo の架空データ専用モードのみ許可します')
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
