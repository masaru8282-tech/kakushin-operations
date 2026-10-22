"""Loopback-only local operator UI. Do not expose directly to the Internet."""
import base64
import binascii
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from .service import run_csv, stock_tool, today
from .master import MASTER, RULE_VERSION

MAX_BODY = 12 * 1024 * 1024


def serve(store, port=8000):
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # No uploaded filenames, bodies, tokens or personal information in logs.
            pass

        def trusted_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

        def send(self, status, data, html=False):
            encoded = data.encode() if html else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'nonce-" + csrf + "'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if not self.trusted_host():
                return self.send(403, {'error': 'Host not allowed'})
            path = urlsplit(self.path).path
            if path == '/':
                html = Path(__file__).with_name('ui.html').read_text().replace('__CSRF__', csrf)
                return self.send(200, html, True)
            if path == '/api/health':
                return self.send(200, dict(status='ok', mode='SHADOW', today=today(), rule_version=RULE_VERSION))
            if path == '/api/master':
                return self.send(200, MASTER)
            if path == '/api/runs':
                return self.send(200, store.list_runs())
            if path.startswith('/api/runs/'):
                result = store.get_run(path.removeprefix('/api/runs/'))
                return self.send(200 if result else 404, result or {'error': '実行が見つかりません'})
            return self.send(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.trusted_host() or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf):
                return self.send(403, {'error': '操作画面から実行してください'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > MAX_BODY:
                    return self.send(413, {'error': '入力は12MB以下で指定してください'})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('JSON object required')
                if self.path == '/api/run':
                    data = base64.b64decode(payload['csv_base64'], validate=True)
                    if len(data) > 8 * 1024 * 1024:
                        return self.send(413, {'error': 'CSVは8MB以下'})
                    result, reused = run_csv(store, data, payload['month'], payload['completeness'], payload['business_date'])
                    return self.send(200, dict(result=result, reused=reused))
                if self.path == '/api/tools':
                    return self.send(200, stock_tool(payload))
                return self.send(404, {'error': 'Not found'})
            except (ValueError, KeyError, TypeError, binascii.Error) as e:
                return self.send(400, {'error': str(e) if isinstance(e, ValueError) else '必須入力・型を確認してください'})
            except Exception:
                return self.send(500, {'error': '処理失敗。入力・ローカルDBの状態を確認してください'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.timeout = 30
    print(f'KAKUSHIN operator server started; loopback port={port}; mode=SHADOW', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
