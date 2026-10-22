"""Start the fictional-data-only demo in Codespaces; never start real operations."""
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent


def health():
    try:
        with urlopen('http://127.0.0.1:8000/api/health', timeout=1) as response:
            return json.load(response)
    except (URLError, OSError, ValueError):
        return None


def main():
    current = health()
    if current:
        if current.get('demo') is True and current.get('mode') == 'SHADOW':
            print('KAKUSHIN demo already ready on port 8000')
            return
        raise SystemExit('Port 8000 is in use by another server; no process was stopped.')
    logs = ROOT / '.runtime'
    logs.mkdir(exist_ok=True, mode=0o700)
    with (logs / 'demo-server.log').open('ab') as output:
        process = subprocess.Popen(
            [sys.executable, '-m', 'kakushin', 'serve', '--demo', '--host', '0.0.0.0', '--port', '8000'],
            cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        if process.poll() is not None:
            raise SystemExit('Demo failed to start; inspect .runtime/demo-server.log')
        if (health() or {}).get('demo') is True:
            print('KAKUSHIN demo ready. Open Ports → 8000 → Open in Browser; keep visibility Private.')
            return
        time.sleep(0.1)
    raise SystemExit('Demo readiness timed out; inspect .runtime/demo-server.log')


if __name__ == '__main__':
    main()
