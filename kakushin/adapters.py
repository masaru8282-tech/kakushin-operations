"""Read-only integration clients. Tokens are caller-provided; never logged."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class IntegrationError(RuntimeError):
    pass


def get_json(url, token):
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={'Authorization': 'Bearer ' + token}), timeout=20) as response:
                return json.load(response)
        except HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise IntegrationError(f'API読取失敗 HTTP {e.code}') from None
            delay = min(5, max(1, int(e.headers.get('Retry-After', '1'))))
            time.sleep(delay * (attempt + 1))
        except (OSError, ValueError):
            raise IntegrationError('API読取失敗。接続・認証・応答形式を確認してください') from None


class SlackReader:
    def __init__(self, token, allowed_channels):
        self.token = token
        self.allowed_channels = set(allowed_channels)

    def history(self, channel, oldest='0'):
        if channel not in self.allowed_channels:
            raise IntegrationError('許可外チャンネル')
        cursor, seen = '', set()
        messages = []
        while True:
            data = get_json('https://slack.com/api/conversations.history?' + urlencode(
                dict(channel=channel, oldest=oldest, cursor=cursor, limit=100)), self.token)
            if not data.get('ok'):
                raise IntegrationError('Slack履歴取得失敗。スコープ・参加状況を確認してください')
            messages.extend(data.get('messages', []))
            cursor = data.get('response_metadata', {}).get('next_cursor', '')
            if not cursor:
                break
            if cursor in seen:
                raise IntegrationError('ページネーションのカーソル重複')
            seen.add(cursor)
        return messages


class SheetsReader:
    ALLOWED = {'1dCOPRzZCDPjmmnG2rpGFRYsUngD7j0XabJs7F-HRXis',
               '1Kom-_V_iZI4xukojyt7y7gepKwmnJ-Pj-q87wKWH5Hc'}

    def __init__(self, access_token):
        self.token = access_token

    def snapshot(self, spreadsheet_id, ranges):
        if spreadsheet_id not in self.ALLOWED or not ranges:
            raise IntegrationError('許可外ファイルまたは読取範囲未指定')
        params = [('includeGridData', 'true')] + [('ranges', r) for r in ranges]
        return get_json('https://sheets.googleapis.com/v4/spreadsheets/' +
                        quote(spreadsheet_id, safe='') + '?' + urlencode(params), self.token)


def verify_slack_signature(secret, timestamp, signature, body, now=None):
    try:
        age = abs((time.time() if now is None else now) - int(timestamp))
    except (ValueError, TypeError):
        return False
    if age > 300:
        return False
    expected = 'v0=' + hmac.new(secret.encode(), b'v0:' + timestamp.encode() + b':' + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
