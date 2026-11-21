import json
import os
from types import SimpleNamespace
import pytest
from test_phase1 import client, load, validate
import store
import engine
import analytics
import scenarios
import groq_adapter
from worker import process_one

@pytest.fixture
def sealed(client):
    load(client)
    assert validate(client)['blockers']==0
    assert client.post('/api/snapshots').status_code==200
    assert process_one()
    return client.get('/api/state').json()['snapshots'][-1]['id']

def overview(client,sid,query=''):
    r=client.get(f'/api/phase2/snapshots/{sid}/overview'+query)
    assert r.status_code==200,r.text
    return r.json()

def baseline(client,sid):
    r=client.get(f'/api/phase2/snapshots/{sid}/scenarios/baseline')
    assert r.status_code==200,r.text
    return r.json()

def test_golden_variance_bridge_coverage_and_sources(client,sealed):
    result=overview(client,sealed)
    assert result['totals']['budget_minor']==100000000
    assert result['totals']['actual_minor']==118000000
    assert result['totals']['variance_minor']==18000000
    assert result['totals']['percentage']==18
    assert {d['label']:d['amount_minor'] for d in result['drivers']}=={'Usage':5000000,'Rate':5500000,'Payroll':6000000,'Supported campaign':1500000,'Other costs':-1000000,'Unexplained':1000000}
    assert result['coverage']=={'numerator_minor':19000000,'denominator_minor':20000000,'percentage':95}
    for f in result['facts']:
        assert sum(d['amount_minor'] for d in result['drivers'] if d['fact_id']==f['id'])==f['variance_minor']
    rate=next(d for d in result['drivers'] if d['kind']=='rate')
    response=client.get(f"/api/phase2/snapshots/{sealed}/evidence/{rate['id']}?limit=2")
    assert response.status_code==200
    evidence=response.json()
    assert evidence['total']==4 and len(evidence['records'])==2
    assert all(r['source']['mapping_version'] for r in evidence['records'])
    assert evidence['artifact']['inputs']['interaction_assigned_to']=='rate'
    assert client.get(evidence['records'][0]['original_url']).json()['original']

def test_scopes_materiality_and_vendor_grain(client,sealed):
    result=overview(client,sealed,'?scope=cumulative')
    assert result['totals']['actual_minor']==sum(r['actual_minor'] for r in result['timeline'])
    assert sum(d['amount_minor'] for d in result['drivers'])==result['totals']['variance_minor']
    mom=overview(client,sealed,'?scope=mom')
    assert mom['totals']['variance_minor']==mom['timeline'][-1]['actual_minor']-mom['timeline'][-2]['actual_minor']
    assert client.get(f'/api/phase2/snapshots/{sealed}/overview?scope=mom&period=2026-06').status_code==422
    engineering=overview(client,sealed,'?department=Engineering')
    assert len(engineering['facts'])==1
    assert engineering['totals']['variance_minor']==10500000
    strict=overview(client,sealed,'?threshold_minor=999999999&executive_override_minor=999999999')
    assert not any(f['material'] for f in strict['facts'])
    detail=client.get(f'/api/phase2/snapshots/{sealed}/transactions?vendor=Vendor%201').json()
    assert detail['total']==1 and detail['records'][0]['amount']==30500000
    assert analytics.percentage(-10000,-50000)==-20
    assert analytics.percentage(100,0) is None

def test_golden_scenario_and_zero_identity(client,sealed):
    b=baseline(client,sealed)
    assert b['baseline']==b['scenario']
    assert b['summary']['baseline_closing_minor']==120000000
    p=dict(b['parameters'],vendor_cut_bps=2000,freeze_position_ids=['POS-001'])
    response=client.post(f'/api/phase2/snapshots/{sealed}/scenarios/calculate',json={'parameters':p})
    assert response.status_code==200,response.text
    r=response.json()
    assert r['summary']['vendor_savings_minor']==110000000
    assert r['summary']['hiring_savings_minor']==100000000
    assert r['summary']['scenario_closing_minor']==330000000
    assert r['payment_schedule'][0]['vendor_saving_minor']==0
    assert all(row['hiring_saving_minor']==0 for row in r['payment_schedule'][:2])
    assert r['assumptions_hash']==b['assumptions_hash']
    assert r['parameters_hash']!=b['parameters_hash']
    assert r['summary']['scenario_first_negative_month'] is None
    saved=client.post(f'/api/phase2/snapshots/{sealed}/scenarios',json={'name':'Golden plan','parameters':p}).json()
    assert saved['result']['id']==r['id']
    packet=client.get(f"/api/phase2/snapshots/{sealed}/packet?scenario_id={saved['id']}").json()
    assert packet['scenario']['assumptions_hash']==r['assumptions_hash']
    assert packet['totals']['unexplained_minor']==1000000
    assert packet['report_status']=='ready_for_report_studio'

def test_partial_month_hires_and_commitment_exclusions(monkeypatch):
    month='2027-02'
    position={'amount':2800000,'data':{'position_id':'P','start_date':'2027-02-15','committed':'false','department':'Engineering'}}
    assert scenarios.position_cost(position,month)==1400000
    assert scenarios.position_cost(position,'2027-01')==0
    manifest={'policy':{'period':'2026-11'}}
    position['data']['committed']='true'
    monkeypatch.setattr(scenarios,'records',lambda *_:(manifest,[dict(position,role='planned_positions',id='p')]))
    with pytest.raises(ValueError,match='cannot be frozen'):scenarios.calculate('demo','sid',{'freeze_position_ids':['P']})

def test_missing_cash_and_legacy_cash_do_not_invent_eligibility(monkeypatch):
    manifest={'policy':{'period':'2026-11'}}
    position={'id':'p','role':'planned_positions','amount':10000000,'data':{'position_id':'P','start_date':'2027-02-01','committed':'false','department':'Engineering'}}
    monkeypatch.setattr(scenarios,'records',lambda *_:(manifest,[position]))
    r=scenarios.calculate('demo','sid',{'freeze_position_ids':['P']})
    assert not r['cash_available'] and r['summary']['scenario_closing_minor'] is None
    assert r['summary']['hiring_savings_minor']==100000000
    with pytest.raises(ValueError,match='No reviewed'):scenarios.calculate('demo','sid',{'vendor_cut_bps':1000})

def test_saved_assumptions_and_bad_inputs(client,sealed):
    b=baseline(client,sealed)
    rows=[]
    for r in b['assumptions']['periods']:
        rows.append({'month':r['month'],'opening_cash_minor':r['opening_cash_minor'],'receipts_minor':r['receipts_minor'],'payments_minor':r['payments_minor'],**{k+'_minor':v for k,v in r['buckets'].items()},'breakdown_reviewed':True,'vendor_cancellable':r['eligible_vendor_minor']>0})
    body={'parameters':b['parameters'],'assumptions':{'periods':rows,'reason':'Reviewed cash buckets against source commitments'}}
    response=client.post(f'/api/phase2/snapshots/{sealed}/scenarios/calculate',json=body)
    assert response.status_code==200,response.text
    assert response.json()['baseline']==b['baseline']
    body['assumptions']['periods'][0]['payments_minor']+=1
    assert client.post(f'/api/phase2/snapshots/{sealed}/scenarios/calculate',json=body).status_code==422
    body['assumptions']['periods'][0]['payments_minor']='malformed'
    assert client.post(f'/api/phase2/snapshots/{sealed}/scenarios/calculate',json=body).status_code==422

def test_owner_notes_do_not_mutate_arithmetic_or_coverage(client,sealed):
    a=overview(client,sealed);f=a['facts'][0]
    r=client.post(f'/api/phase2/snapshots/{sealed}/notes',json={'fact_id':f['id'],'owner':'Engineering lead','text':'Review usage approvals and contract rates.','reviewed':True})
    assert r.status_code==200 and r.json()['evidence_state']=='owner_confirmed'
    b=overview(client,sealed)
    assert a['totals']==b['totals'] and a['coverage']==b['coverage']
    assert len(b['owner_notes'])==1

def test_workspace_and_snapshot_guards(client,sealed):
    a=overview(client,sealed);eid=a['drivers'][0]['id']
    assert client.get(f'/api/phase2/snapshots/another/evidence/{eid}').status_code==404
    client.post('/api/logout')
    assert client.get(f'/api/phase2/snapshots/{sealed}/overview').status_code==401
    assert client.get('/api/phase2/provider').status_code==401

def test_groq_optional_and_wrapper_stream_parameters(client,sealed,monkeypatch):
    a=overview(client,sealed)
    monkeypatch.delenv('GROQ_API_KEY',raising=False)
    assert not client.get('/api/phase2/provider').json()['configured']
    assert client.post(f'/api/phase2/snapshots/{sealed}/investigate',json={'fact_id':a['facts'][0]['id']}).status_code==503
    captured={}
    class Stream:
        def __iter__(self):return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='Review source '))]),SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='evidence.'))])])
        def close(self):pass
    class Client:
        def __init__(self,**kwargs):self.chat=SimpleNamespace(completions=self)
        def create(self,**kwargs):captured.update(kwargs);return Stream()
        def close(self):pass
    monkeypatch.setattr(groq_adapter,'Groq',Client);monkeypatch.setenv('GROQ_API_KEY','test-key-placeholder')
    r=client.post(f'/api/phase2/snapshots/{sealed}/investigate',json={'fact_id':a['facts'][0]['id']})
    events=[json.loads(line) for line in r.text.splitlines()]
    assert ''.join(e['text'] for e in events if e['type']=='delta')=='Review source evidence.'
    assert captured['model']=='openai/gpt-oss-120b' and captured['stream'] is True
    assert captured['reasoning_effort']=='medium' and captured['max_completion_tokens']==2048
    assert 'tools' not in captured


def test_alert_evidence_and_configurable_review_thresholds(client,sealed):
    a=overview(client,sealed,'?jump_percentage=10&jump_amount_minor=100')
    alert=next(x for x in a['alerts'] if x['rule']=='spend_jump')
    assert alert['inputs']['percentage_threshold']==10
    evidence=client.get(f"/api/phase2/snapshots/{sealed}/evidence/{alert['id']}")
    assert evidence.status_code==200
    assert evidence.json()['artifact']['evidence_state']=='investigation_prompt'
    assert {r['id'] for r in evidence.json()['records']}==set(alert['source_ids'])
    strict=overview(client,sealed,'?jump_percentage=9999')
    assert not any(a['rule']=='spend_jump' for a in strict['alerts'])
    assert client.get(f'/api/phase2/snapshots/{sealed}/overview?jump_percentage=nan').status_code==422


def test_reference_close_preserves_all_active_draft_state(client):
    load(client,'defective');validate(client)
    before=store.get('validation','current');uploads=store.all_items('upload');jobs=store.all_items('job')
    response=client.post('/api/phase2/reference')
    assert response.status_code==200,response.text
    assert response.json()['reference'] is True
    assert store.get('validation','current')==before
    assert store.all_items('upload')==uploads and store.all_items('job')==jobs
    sid=response.json()['id']
    assert overview(client,sid)['coverage']['percentage']==95
    assert client.post('/api/phase2/reference').json()['id']==sid
