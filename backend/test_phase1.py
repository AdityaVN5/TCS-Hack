import io
import json
import sys
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).parent))
import store
import engine
import main
from contracts import CONTRACTS
from seed import fixtures
from worker import process_one

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'ROOT',tmp_path)
    monkeypatch.setattr(engine,'ROOT',tmp_path)
    monkeypatch.setattr(main,'ROOT',tmp_path)
    with TestClient(main.app) as c:
        assert c.post('/api/login',json={'email':'demo@trabo.local','password':'trabo-demo-2026'}).status_code==200
        yield c

def load(client,mode='clean'):
    assert client.post('/api/demo',json={'mode':mode}).status_code==200
    for upload in client.get('/api/state').json()['uploads']:
        role=upload['name'].split('.')[0]
        for sheet in upload['sheets']:
            fields=CONTRACTS[role]['required']+CONTRACTS[role]['optional']
            result=client.put('/api/uploads/'+upload['id']+'/mapping',json={'sheet':sheet['name'],'role':role,'columns':{k:k for k in fields if k in sheet['headers']}})
            assert result.status_code==200,result.text

def validate(client):
    client.post('/api/validate'); assert process_one()
    return client.get('/api/state').json()['validation']

def test_clean_reconciliation_and_deterministic_snapshot(client):
    load(client)
    v=validate(client)
    assert v['blockers']==0
    assert v['reconciliation']['raw_equals_accepted_plus_quarantined']
    assert v['reconciliation']['matched_support']+v['reconciliation']['labeled_residual']==v['reconciliation']['ledger']
    assert v['reconciliation']['enriched_ledger']==v['totals']['actuals']['accepted']
    result=engine.get('validation','current')['records']
    assert sum(r['amount'] for r in result if r['role']=='actuals' and r['data']['month']=='2026-11')==118000000
    assert sum(r['amount'] for r in result if r['role']=='budgets' and r['data']['month']=='2026-11')==100000000
    assert client.post('/api/snapshots').status_code==200
    assert process_one()
    s=client.get('/api/state').json()['snapshots'][0]
    assert client.get(f"/api/snapshots/{s['id']}/download").content[:4]==b'PAR1'
    page1=client.get(f"/api/snapshots/{s['id']}/records?limit=100").json()['records']
    page2=client.get(f"/api/snapshots/{s['id']}/records?limit=100&offset=100").json()['records']
    assert len(page1+page2)==s['record_count']
    client.post('/api/snapshots');process_one()
    assert len(client.get('/api/state').json()['snapshots'])==1
    client.post('/api/demo/reset')
    historical=client.get(f"/api/snapshots/{s['id']}/records/{page1[0]['id']}")
    assert historical.status_code==200 and historical.json()['original']
    source_hash=historical.json()['source']['file_hash']
    assert client.get(f"/api/snapshots/{s['id']}/sources/{source_hash}").status_code==200

def test_defects_corrections_and_source_trace(client):
    load(client,'defective');v=validate(client)
    codes={i['code'] for i in v['issues']}
    assert {'invalid_row','duplicate_key','missing_budget','unmatched_invoice','untrusted_text'}.issubset(codes)
    assert client.post('/api/snapshots').status_code==409
    for _ in range(5):
        blockers=[i for i in v['issues'] if i['severity']=='blocker' and i['status']=='open']
        if not blockers:break
        # Resolving the ambiguous date removes its dependent allocation error.
        issue=next((i for i in blockers if i['code']=='invalid_row'),blockers[0])
        source=client.get('/api/records/'+issue['record_id']).json()
        assert source['source']['file_hash'] and source['source']['mapping_version']
        if issue['code']=='invalid_row': action,values='correct',{'date':'2026-06-07'}
        elif issue['code']=='duplicate_key':action,values='exclude',{}
        elif issue['code']=='missing_budget':action,values='approve_zero',{}
        else:pytest.fail(str(issue))
        r=client.post('/api/issues/'+issue['id']+'/resolve',json={'action':action,'values':values,'reason':'Reviewed against original source'})
        assert r.status_code==200,r.text
        process_one();v=client.get('/api/state').json()['validation']
    assert v['blockers']==0
    assert v['warnings']>=2
    assert v['totals']['actuals']['quarantined_rows']==1
    assert client.post('/api/snapshots').status_code==200
    assert process_one()

def test_auth_and_workspace_isolation(client):
    load(client);validate(client)
    rid=engine.get('validation','current')['records'][0]['id']
    assert client.get('/api/records/'+rid).status_code==200
    upload=client.get('/api/state').json()['uploads'][0]
    client.post('/api/logout')
    assert client.get('/api/uploads/'+upload['id']+'/download').status_code==401
    assert client.get('/api/state').status_code==401
    salt='a1'*16
    with store.db() as c:c.execute('INSERT INTO users VALUES(?,?,?,?)',('other@test.local',salt,main.password_hash('test-password',salt),'other'))
    client.post('/api/login',json={'email':'other@test.local','password':'test-password'})
    assert client.get('/api/uploads/'+upload['id']+'/download').status_code==404
    assert client.get('/api/records/'+rid).status_code==404
    store.put('validation',{'id':'current','records':[]},'other')
    assert store.get('validation','current','demo')['records']

def test_stale_mapping_blocks_confirmation_and_preserves_raw(client):
    load(client);validate(client)
    state=client.get('/api/state').json();u=state['uploads'][0]
    before=client.get('/api/uploads/'+u['id']+'/download').content
    m=u['sheets'][0]['mapping'];m['scale']='thousands'
    client.put('/api/uploads/'+u['id']+'/mapping',json=dict(m,sheet='CSV'))
    assert client.get('/api/state').json()['validation']['stale']
    assert client.post('/api/snapshots').status_code==409
    assert client.get('/api/uploads/'+u['id']+'/download').content==before

def test_parser_security_and_formula_cache():
    from parser import parse
    from openpyxl import Workbook
    for filename in ['test.xlsm','test.exe']:
        with pytest.raises(ValueError):parse(b'evil',filename)
    buf=io.BytesIO();wb=Workbook();wb.active.append(['amount']);wb.active.append(['=1+1']);wb.save(buf)
    sheet=parse(buf.getvalue(),'test.xlsx')[0]
    assert sheet['missing_formula_values']==[{'row':2,'column':'amount'}]
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:z.writestr('xl/externalLinks/externalLink1.xml','external')
    with pytest.raises(ValueError,match='external'):parse(buf.getvalue(),'test.xlsx')

def test_scales_credits_dates_and_zero():
    assert engine.money('1.23')==123
    assert engine.money('-50')==-5000
    assert engine.money('2.5','lakhs')==25000000
    assert engine.money('0')==0
    with pytest.raises(ValueError):engine.money('NaN')
    with pytest.raises(ValueError):engine.money('.001')
    with pytest.raises(ValueError,match='Ambiguous'):engine.date_value('06/07/2026')
    assert engine.date_value('06/07/2026','DMY')=='2026-07-06'

def test_idempotent_uploads_and_jobs(client):
    content=fixtures()['actuals.csv']
    a=client.post('/api/uploads',files={'file':('actuals.csv',content,'text/csv')}).json()
    b=client.post('/api/uploads',files={'file':('renamed.csv',content,'text/csv')}).json()
    assert a['id']==b['id']
    j1=client.post('/api/validate').json();j2=client.post('/api/validate').json()
    assert j1['id']==j2['id']
    assert client.post('/api/demo/reset').status_code==409
    assert process_one()

def test_wide_budget_normalization(client):
    data=b'department,account,budget_version,2026-10,2026-11\nEngineering,CLOUD,FY27 Approved,200000,200000\n'
    u=client.post('/api/uploads',files={'file':('wide.csv',data)}).json()
    r=client.put('/api/uploads/'+u['id']+'/mapping',json={'sheet':'CSV','role':'budgets','columns':{'department':'department','account':'account','budget_version':'budget_version'},'wide_months':{'2026-10':'2026-10','2026-11':'2026-11'}})
    assert r.status_code==200,r.text
    validate(client)
    rows=[r for r in store.get('validation','current')['records'] if r['role']=='budgets']
    assert len(rows)==2 and sum(r['amount'] for r in rows)==40000000
    assert len({r['id'] for r in rows})==2


def test_wide_budget_correction_is_isolated_to_one_month(client):
    data=b'department,account,budget_version,2026-10,2026-11\nUnknown,CLOUD,FY27 Approved,100,200\n'
    u=client.post('/api/uploads',files={'file':('wide.csv',data)}).json()
    client.put('/api/uploads/'+u['id']+'/mapping',json={'sheet':'CSV','role':'budgets','columns':{'department':'department','account':'account','budget_version':'budget_version'},'wide_months':{'2026-10':'2026-10','2026-11':'2026-11'}})
    v=validate(client)
    rows=store.get('validation','current')['records']
    october=next(r for r in rows if r['data']['month']=='2026-10')
    issue=next(i for i in v['issues'] if i['record_id']==october['id'])
    response=client.post('/api/issues/'+issue['id']+'/resolve',json={'action':'correct','values':{'department':'Engineering'},'reason':'Reviewed October department'})
    assert response.status_code==200;process_one()
    rows=store.get('validation','current')['records']
    assert next(r for r in rows if r['data']['month']=='2026-10')['accepted']
    assert not next(r for r in rows if r['data']['month']=='2026-11')['accepted']


def test_allocation_overflow_can_be_resolved_from_its_source(client):
    load(client)
    v=validate(client)
    row=next(r for r in store.get('validation','current')['records'] if r['role']=='allocations')
    store.put('correction',{'id':row['id'],'action':'correct','values':{'amount':'9999999'}},'demo')
    v=validate(client)
    issue=next(i for i in v['issues'] if i['code']=='allocation_overflow')
    assert issue['record_id']==row['id'] and 'correct' in issue['actions']
    response=client.post('/api/issues/'+issue['id']+'/resolve',json={'action':'correct','values':{'amount':str(row['amount']/100)},'reason':'Restored reviewed allocation'})
    assert response.status_code==200;process_one()
    assert client.get('/api/state').json()['validation']['blockers']==0


def test_missing_period_and_mixed_currency_block_confirmation(client):
    load(client)
    for u in store.all_items('upload'):
        if u['name']=='actuals.csv':
            u['sheets'][0]['rows']=[r for r in u['sheets'][0]['rows'] if r['date'].startswith('2026-11')]
            u['sheets'][0]['rows'][0]['currency']='USD'
            store.put('upload',u)
    v=validate(client)
    assert any(i['code']=='missing_periods' for i in v['issues'])
    assert any('currency' in i['detail'].lower() for i in v['issues'])
    assert client.post('/api/snapshots').status_code==409


def test_worker_recovery_and_failed_job_retry(client,monkeypatch):
    import worker
    load(client)
    job=client.post('/api/validate').json()
    job['status']='running';store.put('job',job)
    worker.recover_jobs()
    assert store.get('job',job['id'])['status']=='queued'
    def fail(_):raise ValueError('Deliberate recoverable failure')
    with monkeypatch.context() as m:
        m.setattr(worker,'validate',fail)
        assert process_one()
    assert store.get('job',job['id'])['status']=='failed'
    assert client.post('/api/jobs/'+job['id']+'/retry').status_code==200
    assert process_one()
    assert store.get('job',job['id'])['status']=='completed'
    assert store.get('job',job['id'])['attempts']==2


def test_upload_limits_and_multi_sheet_workbooks(monkeypatch):
    import parser
    from openpyxl import Workbook
    with monkeypatch.context() as m:
        m.setattr(parser,'MAX_BYTES',3)
        with pytest.raises(ValueError,match='20 MB'):parser.parse(b'a,b\n1,2','a.csv')
    wb=Workbook();wb.active.title='Budgets';wb.active.append(['month','amount']);wb.active.append(['2026-11',100]);wb.create_sheet('Actuals').append(['journal_id','amount'])
    buf=io.BytesIO();wb.save(buf)
    assert [s['name'] for s in parser.parse(buf.getvalue(),'combined.xlsx')]==['Budgets','Actuals']
    for i in range(19):wb.create_sheet('Extra'+str(i))
    buf=io.BytesIO();wb.save(buf)
    with pytest.raises(ValueError,match='20 sheets'):parser.parse(buf.getvalue(),'too-many.xlsx')


def test_management_commands_seed_and_reset_persistent_drafts(client,monkeypatch):
    import manage
    monkeypatch.setattr(sys,'argv',['manage.py','seed','--mode','clean'])
    manage.main()
    assert len(store.all_items('upload'))==9
    assert all(s.get('mapping') for u in store.all_items('upload') for s in u['sheets'])
    assert process_one()
    assert store.get('validation','current')['blockers']==0
    monkeypatch.setattr(sys,'argv',['manage.py','reset','--yes'])
    manage.main()
    assert not store.all_items('upload')
    assert list(store.ROOT.joinpath('raw','demo').iterdir())
