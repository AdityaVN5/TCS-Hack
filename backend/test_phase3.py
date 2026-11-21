from copy import deepcopy
import io
import json
import pytest
from pypdf import PdfReader
from test_phase1 import client, load, validate
from test_phase2 import sealed
from worker import process_one
import store
import reports
import groq_adapter
from report_content import template_document, validate_document
from report_exports import csv_cell

def draft(client,sid,scenario_id=None,mode='template'):
    response=client.post('/api/reports',json={'snapshot_id':sid,'scenario_id':scenario_id,'mode':mode})
    assert response.status_code==200,response.text
    rid=response.json()['id'];assert process_one()
    value=client.get('/api/reports/'+rid).json()
    assert value['version']['validation']['valid'],value
    return value

def assigned(client,value,owner='Finance lead'):
    doc=deepcopy(value['version']['document'])
    for action in doc['actions']:action['owner']=owner
    r=client.put('/api/reports/'+value['id'],json={'expected_revision':value['revision'],'document':doc})
    assert r.status_code==200,r.text
    return r.json()

def approve(client,value):
    rid=value['id'];rev=value['revision'];sid=value['version']['snapshot_id']
    response=client.post(f'/api/reports/{rid}/submit',json={'expected_revision':rev})
    assert response.status_code==200,response.text
    r=client.post(f'/api/reports/{rid}/approve',json={'expected_revision':rev,'expected_snapshot_id':sid,'meaning_reviewed':True,'unknowns_acknowledged':True,'support_warnings_reviewed':True,'assumptions_reviewed':True,'comment':'Synthetic acceptance test: reviewed figures, meaning, unknowns, and assumptions.'})
    assert r.status_code==200,r.text
    return r.json()

def test_golden_report_review_freeze_and_exports(client,sealed,monkeypatch):
    baseline=client.get(f'/api/phase2/snapshots/{sealed}/scenarios/baseline').json()
    params=dict(baseline['parameters'],vendor_cut_bps=2000,freeze_position_ids=['POS-001'])
    scenario=client.post(f'/api/phase2/snapshots/{sealed}/scenarios',json={'name':'Golden report scenario','parameters':params}).json()
    value=draft(client,sealed,scenario['id']);v=value['version']
    evidence=client.get(f"/api/reports/{value['id']}/evidence/{scenario['id']}").json()
    assert evidence['calculation']['summary']['scenario_closing_minor']==330000000
    assert evidence['total'] and all(r['role'] in ('cash','planned_positions','obligations') for r in evidence['records'])
    coverage=client.get(f"/api/reports/{value['id']}/evidence/coverage").json()
    assert coverage['calculation']['percentage']==95
    assert v['generation']['mode']=='template'
    assert v['validation']['traceability_percentage']==100
    text=json.dumps(v['rendered'])
    assert '180,000.00' in text and '10,000.00' in text
    assert client.get(f"/api/reports/{value['id']}/export/pdf").status_code==409
    value=approve(client,assigned(client,value));rid=value['id']
    assert value['version']['state']=='approved' and value['version']['approval']['reviewer']=='demo@trabo.local'
    monkeypatch.setattr(groq_adapter,'generate_report_json',lambda *_:pytest.fail('Export reran the provider'))
    pdf=client.get(f'/api/reports/{rid}/export/pdf')
    assert pdf.status_code==200 and pdf.content[:4]==b'%PDF'
    extracted='\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert '180,000.00' in extracted and '10,000.00' in extracted and '3,300,000.00' in extracted
    assert 'Finance lead' in extracted and 'SYNTHETIC' not in extracted # normal seeded close
    assert pdf.content==client.get(f'/api/reports/{rid}/export/pdf').content
    import base64
    envelope=client.get(f'/api/reports/{rid}/export/pdf?encoding=base64').json()
    assert base64.b64decode(envelope['content_base64'])==pdf.content
    assert envelope['media_type']=='application/pdf' and envelope['filename'].endswith('.pdf')
    csv=client.get(f'/api/reports/{rid}/export/csv')
    assert csv.status_code==200 and '3300000' in csv.content.decode('utf-8-sig')
    frozen=client.get(f'/api/reports/{rid}/export/json').json()
    assert frozen['payload_hash']==pdf.headers['X-Approved-Payload-SHA256']
    assert frozen['approved_payload']['packet']['scenario']['id']==scenario['id']

def test_invalid_edits_wrong_month_sign_unit_reference_and_omission(client,sealed):
    value=draft(client,sealed);packet=value['version']['packet'];base=value['version']['document']
    mutations=[('amount',lambda d:d['sections'][0]['claims'][0].update(amount_minor=-18000000)),('period',lambda d:d.update(period='2026-10')),('direction',lambda d:d['sections'][0]['claims'][0].update(direction='favorable')),('reference',lambda d:d['sections'][0]['claims'][0].update(reference_id='invented-fact')),('omission',lambda d:d['sections'][3].update(claims=[])),('unsupported_cause',lambda d:d['sections'][0].update(commentary='The overrun was caused by poor management.')),('unbound_claim',lambda d:d['sections'][0].update(commentary='Expenses increased by 77 percent.')),('unbound_relationship',lambda d:d['sections'][0].update(commentary='The expense movement is favorable.'))]
    for code,change in mutations:
        document=deepcopy(base);change(document);result=validate_document(document,packet)
        assert not result['valid'] and any(e['code']==code or (code=='omission' and e['code']=='unknown_disclosure') for e in result['errors']),result
    doc=deepcopy(base);doc['sections'][0]['claims'][0]['amount_minor']=999
    edit=client.put('/api/reports/'+value['id'],json={'expected_revision':1,'document':doc})
    assert edit.status_code==200 and not edit.json()['version']['validation']['valid']
    assert client.post(f"/api/reports/{value['id']}/submit",json={'expected_revision':2}).status_code==409
    assert store.get('fact',packet['facts'][0]['id'])['actual_minor']==packet['facts'][0]['actual_minor']

def test_stale_inputs_and_approved_history(client,sealed):
    value=approve(client,assigned(client,draft(client,sealed)));rid=value['id'];rev=value['revision']
    before=client.get(f'/api/reports/{rid}/export/pdf').content
    u=store.all_items('upload')[0];u['sheets'][0]['mapping']['scale']='thousands';store.put('upload',u)
    read=client.get('/api/reports/'+rid).json()
    assert read['version']['effective_state']=='stale'
    assert client.get(f'/api/reports/{rid}/export/pdf').status_code==409
    historical=client.get(f'/api/reports/{rid}/versions/{rev}/export/pdf?historical=true')
    assert historical.status_code==200 and historical.content==before
    assert not client.get('/api/reports/approved').json()[0]['current']

def test_new_content_revision_does_not_inherit_approval(client,sealed):
    value=approve(client,assigned(client,draft(client,sealed)));rid=value['id'];approved_rev=value['revision']
    doc=deepcopy(value['version']['document']);doc['sections'][0]['commentary']='Review the account evidence with the accountable owner.'
    result=client.put('/api/reports/'+rid,json={'expected_revision':approved_rev,'document':doc})
    assert result.status_code==200 and result.json()['version']['state']=='draft'
    assert client.post(f'/api/reports/{rid}/approve',json={'expected_revision':approved_rev,'expected_snapshot_id':sealed,'comment':'Old revision'}).status_code==409
    old=client.get(f'/api/reports/{rid}/versions/{approved_rev}').json()
    assert old['version']['state']=='approved' and old['version']['historical']

def test_provider_repair_cache_and_fallback(client,sealed,monkeypatch):
    packet=reports.collect_packet('demo',sealed);valid=template_document(packet);calls=[]
    monkeypatch.setenv('GROQ_API_KEY','test-only')
    def response(*args):
        calls.append(args)
        doc=deepcopy(valid)
        if len(calls)==1:doc['period']='1900-01'
        return doc,{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}
    monkeypatch.setattr(groq_adapter,'generate_report_json',response)
    document,log=reports.generation('demo',packet)
    assert log['mode']=='fresh' and len(calls)==2 and log['token_use']['total_tokens']==60
    assert calls[1][2] # semantic errors supplied for one repair
    _,cached=reports.generation('demo',packet)
    assert cached['mode']=='cached' and len(calls)==2
    packet=deepcopy(packet);packet['company']='Different exact packet'
    monkeypatch.setattr(groq_adapter,'generate_report_json',lambda *_:({},None))
    _,fallback=reports.generation('demo',packet)
    assert fallback['mode']=='template' and len(fallback['attempts'])==2

def test_approval_attestations_and_owner_checks(client,sealed):
    value=draft(client,sealed);rid=value['id'];client.post(f'/api/reports/{rid}/submit',json={'expected_revision':1})
    body={'expected_revision':1,'expected_snapshot_id':sealed,'meaning_reviewed':True,'unknowns_acknowledged':True,'support_warnings_reviewed':True,'assumptions_reviewed':True,'comment':'Reviewed synthetic test'}
    assert client.post(f'/api/reports/{rid}/approve',json=body).status_code==422
    value=assigned(client,value);client.post(f'/api/reports/{rid}/submit',json={'expected_revision':2})
    body.update(expected_revision=2,meaning_reviewed=False)
    assert client.post(f'/api/reports/{rid}/approve',json=body).status_code==422
    body.update(meaning_reviewed=True,expected_snapshot_id='wrong-snapshot')
    assert client.post(f'/api/reports/{rid}/approve',json=body).status_code==409

def test_export_escaping_and_workspace_guards(client,sealed):
    assert csv_cell('=HYPERLINK("evil")').startswith("'") and csv_cell(' \t@SUM(A1)').startswith("'")
    assert csv_cell(-123)=='-123'
    value=approve(client,assigned(client,draft(client,sealed),'<script>alert("x")</script>'));rid=value['id']
    pdf=client.get(f'/api/reports/{rid}/export/pdf');assert pdf.status_code==200
    csv=client.get(f'/api/reports/{rid}/export/csv');assert b'<script>' in csv.content # inert CSV text
    client.post('/api/logout')
    assert client.get(f'/api/reports/{rid}').status_code==401
    salt='b2'*16
    import main
    with store.db() as c:c.execute('INSERT INTO users VALUES(?,?,?,?)',('elsewhere@example.local',salt,main.password_hash('other-password',salt),'elsewhere'))
    client.post('/api/login',json={'email':'elsewhere@example.local','password':'other-password'})
    assert client.get(f'/api/reports/{rid}').status_code==404
    assert client.get(f'/api/reports/{rid}/export/pdf').status_code==404

def test_generation_jobs_are_idempotent(client,sealed):
    body={'snapshot_id':sealed,'mode':'template','request_key':'same-request'}
    a=client.post('/api/reports',json=body).json();b=client.post('/api/reports',json=body).json()
    assert a['id']==b['id']
    from worker import recover_jobs
    job=store.get('job',a['job_id']);job['status']='running';store.put('job',job)
    recover_jobs()
    assert store.get('job',a['job_id'])['status']=='queued'
    assert process_one()
    first=reports.complete_generation('demo',a['id'])
    again=reports.complete_generation('demo',a['id'])
    assert first['id']==again['id']


def test_failed_generation_is_visible_and_retries_saved_packet(client,sealed,monkeypatch):
    queued=client.post('/api/reports',json={'snapshot_id':sealed,'mode':'template'}).json()
    original=reports.generation
    def fail(*args):raise RuntimeError('Synthetic internal processing error')
    monkeypatch.setattr(reports,'generation',fail)
    assert process_one()
    failed=client.get('/api/reports/'+queued['id']).json()
    assert failed['generating'] and failed['version'] is None
    job=failed['generation_job']
    assert job['status']=='failed' and 'Synthetic' not in job['error']
    assert client.get('/api/reports').json()[0]['state']=='generation_failed'
    monkeypatch.setattr(reports,'generation',original)
    assert client.post('/api/jobs/'+job['id']+'/retry').status_code==200
    assert process_one()
    recovered=client.get('/api/reports/'+queued['id']).json()
    assert not recovered['generating'] and recovered['version']['revision']==1
    assert recovered['version']['snapshot_id']==sealed


def test_new_corrected_snapshot_requires_new_review(client,sealed):
    import engine
    value=approve(client,assigned(client,draft(client,sealed)));rid=value['id'];rev=value['revision']
    source=next(r for r in store.get('validation','current')['records'] if r['role']=='actuals' and r['data']['month']=='2026-11' and r['data']['account']=='CLOUD')
    store.put('correction',{'id':source['id'],'action':'correct','values':{'amount':'306000'},'reason':'Synthetic corrected cloud posting'})
    assert engine.validate('demo')['blockers']==0
    updated=engine.publish('demo','synthetic-reviewer')
    assert updated['id']!=sealed
    assert client.get('/api/reports/'+rid).json()['version']['effective_state']=='stale'
    assert client.get(f'/api/reports/{rid}/export/pdf').status_code==409
    response=client.post(f'/api/reports/{rid}/regenerate',json={'snapshot_id':updated['id'],'mode':'template','expected_revision':rev})
    assert response.status_code==200,response.text
    assert process_one()
    new=client.get('/api/reports/'+rid).json()
    assert new['version']['state']=='draft' and not new['version']['stale_reasons']
    assert new['version']['packet']['totals']['actual_minor']==118100000
    assert client.get(f'/api/reports/{rid}/versions/{rev}/export/pdf?historical=true').status_code==200


def test_included_scenario_mutation_and_unrelated_scenarios(client,sealed):
    base=client.get(f'/api/phase2/snapshots/{sealed}/scenarios/baseline').json()
    selected=client.post(f'/api/phase2/snapshots/{sealed}/scenarios',json={'name':'Included','parameters':base['parameters']}).json()
    value=approve(client,assigned(client,draft(client,sealed,selected['id'])))
    client.post(f'/api/phase2/snapshots/{sealed}/scenarios',json={'name':'Unrelated','parameters':dict(base['parameters'],vendor_cut_bps=1000)})
    assert not client.get('/api/reports/'+value['id']).json()['version']['stale_reasons']
    changed=store.get('scenario',selected['id']);changed['result']['assumptions_hash']='changed-assumption-version';store.put('scenario',changed)
    assert any('assumptions' in r for r in client.get('/api/reports/'+value['id']).json()['version']['stale_reasons'])
