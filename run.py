"""Start TRABO's API, single worker, and React dev server. Ctrl+C stops all three."""
import os
import subprocess
import sys
import time
from pathlib import Path

root=Path(__file__).parent.resolve()
python=root/('.venv/Scripts/python.exe' if os.name=='nt' else '.venv/bin/python')
if not python.exists():
    raise SystemExit('Create .venv and install backend/requirements.txt first. See README.md.')
processes=[]
try:
    processes.append(subprocess.Popen([str(python),'-m','uvicorn','main:app','--host','127.0.0.1','--port','8000'],cwd=root/'backend'))
    processes.append(subprocess.Popen([str(python),'worker.py'],cwd=root/'backend'))
    npm='npm.cmd' if os.name=='nt' else 'npm'
    processes.append(subprocess.Popen([npm,'run','dev'],cwd=root/'frontend',shell=os.name=='nt'))
    print('\nTRABO: http://127.0.0.1:5173 | API: http://127.0.0.1:8000/docs\n',flush=True)
    while all(p.poll() is None for p in processes):time.sleep(.5)
except KeyboardInterrupt:
    pass
finally:
    for p in reversed(processes):
        if p.poll() is None:
            if os.name=='nt':subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            else:p.terminate()
