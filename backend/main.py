import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
import duckdb
from fastapi import FastAPI, Depends, HTTPException, UploadFile, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from store import ROOT, init, db, get, put, all_items, clear, workspace, now, DEFAULT_WORKSPACE
from contracts import CONTRACTS
from parser import parse, MAX_BYTES
from engine import input_fingerprint
from worker import enqueue
from seed import fixtures

def password_hash(password,salt):
    return hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),300000).hex()

@asynccontextmanager
async def lifespan(app):
    init(); workspace()
    email=os.getenv('TRABO_EMAIL','demo@trabo.local')
    with db() as c:
        if not c.execute('SELECT email FROM users WHERE email=?',(email,)).fetchone():
            salt=secrets.token_hex(16)
            c.execute('INSERT INTO users VALUES(?,?,?,?)',(email,salt,password_hash(os.getenv('TRABO_PASSWORD','trabo-demo-2026'),salt),'demo'))
    yield

app=FastAPI(title='TRABO · Phases 1 & 2',lifespan=lifespan)

def auth(request:Request):
    token=request.cookies.get('trabo_session','')
    with db() as c:
        row=c.execute('SELECT u.email,u.workspace FROM sessions s JOIN users u ON u.email=s.email WHERE s.token=? AND s.expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
    if not row: raise HTTPException(401,'Sign in to your workspace.')
    return dict(row)

def wid(user=Depends(auth)): return user['workspace']

def require(kind,key,w):
    item=get(kind,key,w)
    if not item: raise HTTPException(404,'Record not found in this workspace.')
    return item

def assert_idle(w):
    if any(j['status'] in ('queued','running') for j in all_items('job',w)):
        raise HTTPException(409,'Wait for the current job to finish before changing inputs.')

class Login(BaseModel):
    email:str
    password:str

@app.post('/api/login')
def login(body:Login,response:Response,request:Request):
    # Small persistent rate limit for the local password endpoint.
    key=hashlib.sha256((request.client.host+body.email).encode()).hexdigest()
    attempt=get('login_attempt',key,'auth') or {'id':key,'times':[]}
    attempt['times']=[t for t in attempt['times'] if t>time.time()-60]
    if len(attempt['times'])>=10: raise HTTPException(429,'Too many attempts. Try again in one minute.')
    attempt['times'].append(time.time()); put('login_attempt',attempt,'auth')
    with db() as c:
        row=c.execute('SELECT * FROM users WHERE email=?',(body.email,)).fetchone()
        if not row or not hmac.compare_digest(row['password'],password_hash(body.password,row['salt'])): raise HTTPException(401,'Email or password is incorrect.')
        token=secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),body.email,time.time()+86400))
    response.set_cookie('trabo_session',token,httponly=True,samesite='strict',secure=os.getenv('TRABO_SECURE_COOKIE')=='1',max_age=86400)
    return {'email':body.email}

@app.post('/api/logout')
def logout(request:Request,response:Response):
    with db() as c: c.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(request.cookies.get('trabo_session','').encode()).hexdigest(),))
    response.delete_cookie('trabo_session'); return {'ok':True}

@app.get('/api/health')
def health(): return {'status':'ok','service':'TRABO API'}

@app.get('/api/state')
def state(user=Depends(auth)):
    w=user['workspace']; validation=get('validation','current',w)
    if validation:
        validation={k:v for k,v in validation.items() if k!='records'}
        validation['stale']=validation['fingerprint']!=input_fingerprint(w)
        current_ids={i['id'] for i in validation['issues']}
        validation['issues'] += [i for i in all_items('issue_history',w) if i['id'] not in current_ids]
    uploads=all_items('upload',w)
    for upload in uploads:
        for sheet in upload['sheets']:
            sheet['preview']=sheet.pop('rows')[:5]
    return {'user':user,'workspace':workspace(w),'contracts':CONTRACTS,'uploads':uploads,'validation':validation,'jobs':all_items('job',w),'snapshots':all_items('snapshot',w),'mapping_revisions':all_items('mapping_revision',w)}

class Policy(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    entity:str=Field(min_length=1,max_length=60,pattern=r'^[A-Za-z0-9_-]+$')
    period:str=Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
    budget_version:str=Field(min_length=1,max_length=100)
    basis:Literal['accrual','cash']='accrual'
    departments:list[str]
    accounts:list[str]
    aliases:dict[str,str]={}

@app.put('/api/workspace')
def policy(body:Policy,w=Depends(wid)):
    assert_idle(w)
    return put('workspace',dict(DEFAULT_WORKSPACE,**body.model_dump(),id=w),w)

def add_upload(data,name,w):
    hashed=hashlib.sha256(data).hexdigest()
    uid=hashlib.sha256((w+hashed).encode()).hexdigest()[:24]
    existing=get('upload',uid,w)
    if existing: return existing
    try: sheets=parse(data,name)
    except ValueError as e: raise HTTPException(422,str(e))
    rawdir=ROOT/'raw'/w
    rawdir.mkdir(parents=True,exist_ok=True)
    path=rawdir/hashed
    if not path.exists():
        with path.open('xb') as f: f.write(data)
    return put('upload',{'id':uid,'hash':hashed,'name':Path(name).name,'size':len(data),'created_at':now(),'sheets':sheets},w)

@app.post('/api/uploads')
async def upload(file:UploadFile,w=Depends(wid)):
    assert_idle(w)
    data=await file.read(MAX_BYTES+1)
    item=add_upload(data,file.filename or 'upload.csv',w)
    return {'id':item['id'],'name':item['name']}

@app.get('/api/uploads/{uid}/download')
def download(uid:str,w=Depends(wid)):
    item=require('upload',uid,w)
    return FileResponse(ROOT/'raw'/w/item['hash'],filename=item['name'],media_type='application/octet-stream')

class Mapping(BaseModel):
    sheet:str
    role:str
    columns:dict[str,str]
    scale:Literal['rupees','paise','thousands','lakhs']='rupees'
    sign:Literal['positive','negative']='positive'
    date_format:Literal['ISO','DMY','MDY']='ISO'
    wide_months:dict[str,str]={}

@app.put('/api/uploads/{uid}/mapping')
def mapping(uid:str,body:Mapping,w=Depends(wid),user=Depends(auth)):
    assert_idle(w); item=require('upload',uid,w)
    sheet=next((s for s in item['sheets'] if s['name']==body.sheet),None)
    if not sheet or body.role not in CONTRACTS: raise HTTPException(422,'Choose an existing sheet and supported role.')
    required=set(CONTRACTS[body.role]['required'])
    if body.wide_months and body.role=='budgets': required-= {'month','amount'}
    elif body.wide_months: raise HTTPException(422,'Wide columns are supported for budgets only.')
    if not required.issubset({k for k,v in body.columns.items() if v}) or any(v not in sheet['headers'] for v in body.columns.values() if v): raise HTTPException(422,'Map every required field to a valid source column.')
    if any(v not in sheet['headers'] for v in body.wide_months.values()): raise HTTPException(422,'Invalid wide month column.')
    from engine import date_value
    try:
        for month in body.wide_months: date_value(month)
    except ValueError: raise HTTPException(422,'Wide month keys must be YYYY-MM.')
    value=body.model_dump(exclude={'sheet'})
    from store import digest
    revision={'id':digest([uid,body.sheet,value]),'upload_id':uid,'sheet':body.sheet,'mapping':value,'confirmed_at':now(),'reviewer':user['email']}
    put('mapping_revision',revision,w)
    sheet['mapping']=value; put('upload',item,w)
    return revision

@app.post('/api/validate')
def validate_api(w=Depends(wid)): return enqueue(w,'validate')

@app.post('/api/snapshots')
def snapshot_api(w=Depends(wid),user=Depends(auth)):
    assert_idle(w)
    v=get('validation','current',w)
    if not v or v['fingerprint']!=input_fingerprint(w) or v['blockers']: raise HTTPException(409,'Run current validation and resolve all blockers first.')
    return enqueue(w,'snapshot',user['email'])

@app.get('/api/jobs/{jid}')
def job(jid:str,w=Depends(wid)): return require('job',jid,w)

@app.post('/api/jobs/{jid}/retry')
def retry(jid:str,w=Depends(wid)):
    assert_idle(w); item=require('job',jid,w)
    if item['status']!='failed': raise HTTPException(409,'Only failed jobs can be retried.')
    item.update(status='queued',error=None); return put('job',item,w)

@app.get('/api/records/{rid}')
def record(rid:str,w=Depends(wid)):
    v=require('validation','current',w)
    r=next((r for r in v['records'] if r['id']==rid),None)
    if not r: raise HTTPException(404,'Source record not found.')
    upload=next((u for u in all_items('upload',w) if u['hash']==r['source']['file_hash']),None)
    sheet=next(s for s in upload['sheets'] if s['name']==r['source']['sheet']) if upload else None
    return dict(r,original=sheet['rows'][r['source']['row']-2] if sheet else {},upload_id=upload and upload['id'])

class Correction(BaseModel):
    action:Literal['correct','exclude','approve_zero','acknowledge']
    values:dict[str,str]={}
    reason:str=Field(min_length=3,max_length=1000)

@app.post('/api/issues/{iid}/resolve')
def resolve(iid:str,body:Correction,w=Depends(wid),user=Depends(auth)):
    assert_idle(w); v=require('validation','current',w)
    issue=next((i for i in v['issues'] if i['id']==iid),None)
    if not issue or body.action not in issue['actions']: raise HTTPException(422,'This correction action is unavailable.')
    if body.action=='correct' and not body.values: raise HTTPException(422,'Enter at least one corrected field.')
    key=issue['record_id'] if body.action in ('correct','exclude') else iid
    prior=get('correction',key,w) or {}
    values=dict(prior.get('values',{}),**body.values)
    correction={'id':key,**body.model_dump(),'values':values,'reviewed_at':now(),'reviewer':user['email']}
    put('correction',correction,w)
    from store import digest
    put('correction_revision',dict(correction,id=digest(correction),record_or_issue_id=key),w)
    return enqueue(w,'validate')

@app.get('/api/snapshots/{sid}/manifest')
def manifest(sid:str,w=Depends(wid)): return require('snapshot',sid,w)

@app.get('/api/snapshots/{sid}/records')
def snapshot_records(sid:str,offset:int=0,limit:int=50,role:str|None=None,w=Depends(wid)):
    require('snapshot',sid,w)
    if offset<0 or not 1<=limit<=100: raise HTTPException(422,'Invalid pagination.')
    if role is not None and role not in CONTRACTS: raise HTTPException(422,'Unknown data role.')
    con=duckdb.connect()
    try:
        path=str(ROOT/'snapshots'/w/sid/'canonical.parquet')
        total=con.execute('SELECT count(*) FROM read_parquet(?) WHERE (? IS NULL OR role=?)',[path,role,role]).fetchone()[0]
        rows=con.execute('SELECT payload FROM read_parquet(?) WHERE (? IS NULL OR role=?) ORDER BY record_id LIMIT ? OFFSET ?',[path,role,role,limit,offset]).fetchall()
        return {'snapshot_id':sid,'offset':offset,'total':total,'records':[json.loads(r[0]) for r in rows]}
    finally: con.close()

@app.get('/api/snapshots/{sid}/download')
def snapshot_download(sid:str,w=Depends(wid)):
    require('snapshot',sid,w)
    return FileResponse(ROOT/'snapshots'/w/sid/'canonical.parquet',filename=f'trabo-{sid[:12]}.parquet')

@app.get('/api/snapshots/{sid}/sources/{file_hash}')
def snapshot_source(sid:str,file_hash:str,w=Depends(wid)):
    snapshot=require('snapshot',sid,w)
    source=next((s for s in snapshot['inputs'] if s['hash']==file_hash),None)
    if not source: raise HTTPException(404,'Source does not belong to this snapshot.')
    return FileResponse(ROOT/'raw'/w/file_hash,filename=source['name'],media_type='application/octet-stream')

@app.get('/api/snapshots/{sid}/records/{rid}')
def snapshot_record(sid:str,rid:str,w=Depends(wid)):
    require('snapshot',sid,w)
    con=duckdb.connect()
    try:
        row=con.execute('SELECT payload FROM read_parquet(?) WHERE record_id=?',[str(ROOT/'snapshots'/w/sid/'canonical.parquet'),rid]).fetchone()
    finally: con.close()
    if not row: raise HTTPException(404,'Record does not belong to this snapshot.')
    record=json.loads(row[0]);source=record['source']
    sheets=parse((ROOT/'raw'/w/source['file_hash']).read_bytes(),source['filename'])
    sheet=next(s for s in sheets if s['name']==source['sheet'])
    return dict(record,original=sheet['rows'][source['row']-2],snapshot_id=sid)

class Demo(BaseModel):
    mode:Literal['clean','defective']='defective'

@app.post('/api/demo')
def demo(body:Demo,w=Depends(wid)):
    assert_idle(w)
    if all_items('upload',w): raise HTTPException(409,'Reset demo inputs before loading another collection.')
    for name,data in fixtures(body.mode=='defective').items(): add_upload(data,name,w)
    return {'ok':True}

@app.post('/api/demo/reset')
def reset(w=Depends(wid)):
    assert_idle(w)
    # Preserve immutable raw files and sealed snapshots; reset only draft workflow state.
    for kind in ('upload','validation','correction','correction_revision','issue_history','mapping_revision','job'): clear(kind,w)
    return {'ok':True}

from phase2_api import router_for
app.include_router(router_for(auth,wid))

# Optional single-origin production serving after `npm run build`.
from fastapi.staticfiles import StaticFiles
frontend_dist=Path(__file__).parent.parent/'frontend'/'dist'
if frontend_dist.is_dir():
    app.mount('/',StaticFiles(directory=frontend_dist,html=True),name='frontend')
