"""Build a labeled synthetic close in isolation, preserving all active draft objects."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import store
from store import get, put

def create_reference(wid,actor):
    staging=store.ROOT/'staging';staging.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='reference-',dir=staging) as directory:
        env=dict(os.environ,TRABO_DATA=str(Path(directory).resolve()),TRABO_REFERENCE_ACTOR=actor)
        process=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--build',wid],env=env,capture_output=True,text=True,timeout=60)
        if process.returncode:raise ValueError('The reference close could not be built. Your draft is unchanged.')
        manifest=json.loads((Path(directory)/'handoff.json').read_text(encoding='utf-8'));sid=manifest['id']
        existing=get('snapshot',sid,wid)
        if existing:return existing
        raw=store.ROOT/'raw'/wid;raw.mkdir(parents=True,exist_ok=True)
        for item in manifest['inputs']:
            target=raw/item['hash']
            if not target.exists():shutil.copyfile(Path(directory)/'raw'/wid/item['hash'],target)
        snapshot=store.ROOT/'snapshots'/wid/sid;snapshot.mkdir(parents=True,exist_ok=True)
        for name in ('canonical.parquet','manifest.json'):
            shutil.copyfile(Path(directory)/'snapshots'/wid/sid/name,snapshot/name)
        return put('snapshot',manifest,wid)

def build(wid):
    from main import add_upload, mapping, Mapping
    from seed import fixtures
    from contracts import CONTRACTS
    from worker import enqueue, process_one
    store.init();store.workspace(wid)
    for name,content in fixtures().items():
        upload=add_upload(content,name,wid);role=name.removesuffix('.csv');contract=CONTRACTS[role]
        for sheet in upload['sheets']:
            mapping(upload['id'],Mapping(sheet=sheet['name'],role=role,columns={k:k for k in contract['required']+contract['optional'] if k in sheet['headers']}),wid,{'email':os.environ['TRABO_REFERENCE_ACTOR']})
    enqueue(wid,'validate');process_one()
    if get('validation','current',wid)['blockers']:raise ValueError('Reference validation failed.')
    enqueue(wid,'snapshot',os.environ['TRABO_REFERENCE_ACTOR']);process_one()
    manifest=store.all_items('snapshot',wid)[-1]
    manifest.update(reference=True,label='Phase 2 · Synthetic reference close')
    path=store.ROOT/'snapshots'/wid/manifest['id']/'manifest.json'
    path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    (store.ROOT/'handoff.json').write_text(json.dumps(manifest),encoding='utf-8')

if __name__=='__main__' and len(sys.argv)==3 and sys.argv[1]=='--build':build(sys.argv[2])
