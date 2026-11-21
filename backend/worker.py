"""Run exactly one worker: python worker.py. Jobs survive API/worker restarts."""
import json
import time
import uuid
import os
import threading
from store import init, db, get, put, all_items, now
from engine import validate, publish

def enqueue(wid, kind, actor=None, payload=None):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        jobs=[json.loads(r['body']) for r in c.execute("SELECT body FROM objects WHERE kind='job' AND workspace=?",(wid,))]
        pending=[j for j in jobs if j['kind']==kind and j.get('payload',{})==(payload or {}) and j['status'] in ('queued','running')]
        if pending: return pending[-1]
        job={'id':uuid.uuid4().hex,'kind':kind,'status':'queued','created_at':now(),'attempts':0}
        job['actor']=actor
        job['payload']=payload or {}
        c.execute('INSERT INTO objects VALUES(?,?,?,?)',('job',job['id'],wid,json.dumps(job)))
    return job

def process_one():
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        rows=c.execute("SELECT workspace, body FROM objects WHERE kind='job'").fetchall()
        found=next(((r['workspace'],json.loads(r['body'])) for r in rows if json.loads(r['body'])['status']=='queued'),None)
        if not found: return False
        wid,job=found
        job.update(status='running',attempts=job['attempts']+1,started_at=now())
        c.execute("UPDATE objects SET body=? WHERE kind='job' AND id=? AND workspace=?",(json.dumps(job),job['id'],wid))
    try:
        if job['kind']=='report':
            from reports import complete_generation
            result=complete_generation(wid,job['payload']['report_id'])
        else:result=publish(wid,job.get('actor')) if job['kind']=='snapshot' else validate(wid)
        job.update(status='completed',result_id=result['id'],finished_at=now())
    except Exception as e:
        job.update(status='failed',error=str(e) if isinstance(e,ValueError) else 'Processing failed. Retry the job or check the worker logs.',finished_at=now())
        import traceback
        traceback.print_exc()
    put('job',job,wid)
    return True

def recover_jobs():
    with db() as c:
        for row in c.execute("SELECT id,workspace,body FROM objects WHERE kind='job'").fetchall():
            job=json.loads(row['body'])
            if job['status']=='running':
                job['status']='queued'
                c.execute("UPDATE objects SET body=? WHERE kind='job' AND id=? AND workspace=?",(json.dumps(job),job['id'],row['workspace']))

def run():
    init()
    # Hold an OS lock for the process lifetime; a second worker cannot recover live jobs.
    from store import ROOT
    lock=(ROOT/'worker.lock').open('a+b')
    lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        raise SystemExit('A TRABO worker is already running for this data directory.')
    # Only one process should run this worker. Recover interrupted operations idempotently.
    recover_jobs()
    def heartbeat():
        while True:
            try:put('health',{'id':'worker','epoch':time.time(),'pid':os.getpid(),'seen_at':now()},'_system')
            except Exception:pass
            time.sleep(3)
    threading.Thread(target=heartbeat,daemon=True).start()
    print('TRABO worker ready',flush=True)
    while True:
        if not process_one(): time.sleep(.5)

if __name__=='__main__': run()
