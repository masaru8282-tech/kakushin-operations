"""Start the private read-only review UI in Codespaces; demo elsewhere.

The historical filename is retained so existing Codespaces can upgrade in one step.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import secrets
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


def stop_owned_server():
    """Only restart this checkout's exact Python CLI, never an unknown listener."""
    owned=[]
    control={}
    try:control=json.loads((ROOT/'.runtime/server-control.json').read_text())
    except (OSError,ValueError):pass
    current=health() or {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():continue
        try:
            args=(entry/'cmdline').read_bytes().split(b'\0')
            if len(args)<5 or not Path(os.fsdecode(args[0])).name.startswith('python'):continue
            if args[1:4]!=[b'-m',b'kakushin',b'serve']:continue
            if b'--port' not in args or args[args.index(b'--port')+1]!=b'8000':continue
            if not (b'--demo' in args or b'--review' in args):continue
            has_control=(control.get('pid')==int(entry.name) and control.get('instance') and control.get('instance')==current.get('instance'))
            if not has_control and (entry/'cwd').resolve()!=ROOT:continue
            owned.append(int(entry.name))
        except (OSError,IndexError):continue
    if len(owned)!=1:
        raise SystemExit('Cannot safely identify this checkout\'s server. No process was stopped.')
    os.kill(owned[0],signal.SIGTERM)
    for _ in range(50):
        if not Path('/proc',str(owned[0])).exists() or health() is None:return
        time.sleep(.1)
    raise SystemExit('Previous server did not stop; no additional process was changed.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--restart',action='store_true',help='このcheckoutの既知サーバーだけを再起動')
    args=parser.parse_args()
    review=bool(os.environ.get('CODESPACE_NAME'))
    current=health()
    if current:
        desired=current.get('review') is True if review else current.get('demo') is True
        if desired and current.get('mode')=='SHADOW' and not args.restart:
            print('KAKUSHIN already ready on port 8000')
            return
        if not args.restart:
            raise SystemExit('An older server is running. Run: python3 .devcontainer/start_demo.py --restart')
        if current.get('mode')!='SHADOW' or not (current.get('demo') or current.get('review')):
            raise SystemExit('Port 8000 belongs to an unknown service; no process was stopped.')
        stop_owned_server()
    logs=ROOT/'.runtime';logs.mkdir(exist_ok=True,mode=0o700)
    instance=secrets.token_hex(16)
    environment=os.environ.copy();environment['KAKUSHIN_SERVER_INSTANCE']=instance
    with (logs/'demo-server.log').open('ab') as output:
        process=subprocess.Popen([sys.executable,'-m','kakushin','serve','--review' if review else '--demo',
                                  '--host','0.0.0.0','--port','8000'],
            cwd=ROOT,stdout=output,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True,env=environment)
    for _ in range(100):
        if process.poll() is not None:raise SystemExit('Startup failed; inspect .runtime/demo-server.log')
        ready=health() or {}
        if ready.get('review' if review else 'demo') is True:
            control=logs/'server-control.json'
            with os.fdopen(os.open(control,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as output:
                json.dump({'pid':process.pid,'instance':instance},output)
            print('KAKUSHIN ready. Ports → 8000 → Open in Browser; visibility must remain Private.')
            return
        time.sleep(.1)
    raise SystemExit('Readiness timeout; inspect .runtime/demo-server.log')


if __name__=='__main__':main()
