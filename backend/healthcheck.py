"""Read-only liveness check used by Docker health checks."""
import argparse
import sys
import time
import urllib.request
from store import get

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--worker',action='store_true');args=parser.parse_args()
    try:
        if args.worker:
            beat=get('health','worker','_system')
            if not beat or time.time()-beat['epoch']>20:raise ValueError('Worker heartbeat is stale.')
        else:
            with urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3) as response:
                if response.status!=200:raise ValueError('API is unavailable.')
    except Exception:
        print('Unhealthy',file=sys.stderr);raise SystemExit(1)
    print('Healthy')

if __name__=='__main__':main()
