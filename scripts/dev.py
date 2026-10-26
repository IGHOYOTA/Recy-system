"""Local portal server. Loads .env without shell evaluation; never applies migrations."""
import os
import sys
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
for line in (root/'.env').read_text().splitlines() if (root/'.env').exists() else []:
    line=line.strip()
    if line and not line.startswith('#') and '=' in line:
        key,value=line.split('=',1)
        os.environ.setdefault(key.strip(),value.strip().strip('\"').strip("'"))
os.environ['RECY_COOKIE_SECURE']='0'  # This server binds only to loopback HTTP.
from recy.wsgi import application

class LocalServer(ThreadingMixIn,WSGIServer):
    daemon_threads=True

if __name__=='__main__':
    server=make_server('127.0.0.1',8000,application,server_class=LocalServer)
    print('Recy portal: http://127.0.0.1:8000 (Ctrl+C to stop)',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
