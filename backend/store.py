import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / '.env', override=False)

ROOT = Path(os.environ.get('TRABO_DATA', Path(__file__).parent / 'data'))

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

@contextmanager
def db():
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ROOT / 'workflow.sqlite', timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS objects(kind TEXT, id TEXT, workspace TEXT, body TEXT, PRIMARY KEY(kind,id,workspace));
        CREATE TABLE IF NOT EXISTS users(email TEXT PRIMARY KEY, salt TEXT, password TEXT, workspace TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, email TEXT, expires REAL);
        ''')
        # Preserve existing local data when upgrading the draft schema.
        if not next(r for r in c.execute('PRAGMA table_info(objects)') if r['name']=='workspace')['pk']:
            c.executescript('''
            BEGIN IMMEDIATE;
            ALTER TABLE objects RENAME TO objects_legacy;
            CREATE TABLE objects(kind TEXT, id TEXT, workspace TEXT, body TEXT, PRIMARY KEY(kind,id,workspace));
            INSERT INTO objects SELECT * FROM objects_legacy;
            DROP TABLE objects_legacy;
            COMMIT;
            ''')

def put(kind, value, workspace='demo'):
    with db() as c:
        c.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)', (kind, value['id'], workspace, json.dumps(value)))
    return value

def get(kind, key, workspace='demo'):
    with db() as c:
        row = c.execute('SELECT body FROM objects WHERE kind=? AND id=? AND workspace=?', (kind,key,workspace)).fetchone()
    return json.loads(row['body']) if row else None

def all_items(kind, workspace='demo'):
    with db() as c:
        rows = c.execute('SELECT body FROM objects WHERE kind=? AND workspace=? ORDER BY rowid', (kind,workspace)).fetchall()
    return [json.loads(row['body']) for row in rows]

def clear(kind, workspace='demo'):
    with db() as c:
        c.execute('DELETE FROM objects WHERE kind=? AND workspace=?', (kind,workspace))

DEFAULT_WORKSPACE = {'id':'demo','name':'Acme Technologies','entity':'ACME-IN','currency':'INR','period':'2026-11','budget_version':'FY27 Approved','basis':'accrual','amount_scale':'rupees','expense_sign':'positive','departments':['Engineering','People','Marketing','Operations'],'accounts':['CLOUD','PAYROLL','MARKETING','OTHER'],'aliases':{'Engg':'Engineering'}}

def workspace(wid='demo'):
    return get('workspace',wid,wid) or put('workspace',dict(DEFAULT_WORKSPACE,id=wid),wid)
