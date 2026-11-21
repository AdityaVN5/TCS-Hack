"""Build an isolated synthetic handoff, measure cold/warm operations, and run the narrative corpus."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='artifacts/release');args=parser.parse_args()
    output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    run_dir=output/'runs'/uuid.uuid4().hex;run_dir.mkdir(parents=True)
    os.environ['TRABO_DATA']=str(run_dir)
    os.environ['TRABO_EMAIL']='synthetic-reviewer@trabo.local'
    os.environ['TRABO_PASSWORD']=secrets.token_urlsafe(24)
    from fastapi.testclient import TestClient
    import main as api
    from seed import fixtures
    from contracts import CONTRACTS
    from worker import process_one
    from store import workspace,put,get,digest
    from engine import validate,publish
    import analytics
    import scenarios
    import reports
    from narrative_cases import cases
    from report_content import validate_document
    from report_exports import report_pdf
    timings={}
    def timed(name,fn,target):
        start=time.perf_counter();value=fn();elapsed=round((time.perf_counter()-start)*1000,2)
        timings[name]={'milliseconds':elapsed,'target_milliseconds':target,'target_met':elapsed<target}
        print(f'{name}: {elapsed} ms',flush=True)
        return value
    def load(wid,files):
        workspace(wid)
        for role,content in files.items():
            role=role.removesuffix('.csv');u=api.add_upload(content,role+'.csv',wid)
            c=CONTRACTS[role]
            for sheet in u['sheets']:api.mapping(u['id'],api.Mapping(sheet=sheet['name'],role=role,columns={k:k for k in c['required']+c['optional'] if k in sheet['headers']}),wid,{'email':'synthetic-acceptance-runner'})
    with TestClient(api.app) as client:
        login=client.post('/api/login',json={'email':os.environ['TRABO_EMAIL'],'password':os.environ['TRABO_PASSWORD']});assert login.status_code==200
        load('demo',fixtures())
        assert validate('demo')['blockers']==0
        snapshot=publish('demo','synthetic-acceptance-runner');sid=snapshot['id']
        analysis=timed('dashboard_cold',lambda:analytics.build('demo',sid),500)
        assert analysis['totals']['variance_minor']==18000000 and analysis['coverage']['percentage']==95
        timed('dashboard_warm',lambda:analytics.build('demo',sid),500)
        baseline=scenarios.calculate('demo',sid)
        params=dict(baseline['parameters'],vendor_cut_bps=2000,freeze_position_ids=['POS-001'])
        calc=timed('scenario_cold',lambda:scenarios.calculate('demo',sid,params),300)
        timed('scenario_warm',lambda:scenarios.calculate('demo',sid,params),300)
        assert calc['summary']['scenario_closing_minor']==330000000
        saved=client.post(f'/api/phase2/snapshots/{sid}/scenarios',json={'name':'Synthetic golden plan','parameters':params}).json()
        queued=client.post('/api/reports',json={'snapshot_id':sid,'scenario_id':saved['id'],'mode':'template','title':'TRABO · Synthetic acceptance report'}).json();rid=queued['id']
        timed('narrative_template_cold',process_one,20000)
        value=client.get('/api/reports/'+rid).json();doc=value['version']['document']
        warm=client.post('/api/reports',json={'snapshot_id':sid,'scenario_id':saved['id'],'mode':'template','title':'Synthetic warm generation benchmark'})
        assert warm.status_code==200
        timed('narrative_template_warm',process_one,20000)
        for action in doc['actions']:action['owner']='Synthetic finance review team'
        edited=client.put('/api/reports/'+rid,json={'expected_revision':value['revision'],'document':doc}).json();rev=edited['revision']
        assert client.post(f'/api/reports/{rid}/submit',json={'expected_revision':rev}).status_code==200
        approval=client.post(f'/api/reports/{rid}/approve',json={'expected_revision':rev,'expected_snapshot_id':sid,'meaning_reviewed':True,'unknowns_acknowledged':True,'support_warnings_reviewed':True,'assumptions_reviewed':True,'comment':'Synthetic automated acceptance demonstration only. Figures, source links, unknowns, and timing assumptions are asserted by the test harness; this is not a real-company financial approval.'})
        assert approval.status_code==200,approval.text
        payload,hashed=reports.approved_payload('demo',rid)
        pdf=timed('approved_pdf_cold',lambda:report_pdf(payload,hashed),10000)
        assert timed('approved_pdf_warm',lambda:report_pdf(payload,hashed),10000)==pdf
        (output/'approved-sample.pdf').write_bytes(pdf)
        (output/'approved-financial-tables.csv').write_bytes(client.get(f'/api/reports/{rid}/export/csv').content)
        (output/'approved-payload.json').write_bytes(client.get(f'/api/reports/{rid}/export/json').content)
        (output/'snapshot-manifest.json').write_text(json.dumps(snapshot,indent=2),encoding='utf-8')
        packet=value['version']['packet'];corpus=cases(packet);evaluation=[]
        for case in corpus:
            started=time.perf_counter();result=validate_document(case['document'],packet)
            evaluation.append({'label':case['label'],'expected_valid':case['expected_valid'],'observed_valid':result['valid'],'matched_expected':result['valid']==case['expected_valid'],'errors':result['errors'],'latency_ms':round((time.perf_counter()-started)*1000,3),'provider_tokens':0})
        assert all(c['matched_expected'] for c in evaluation)
        (output/'narrative-cases.json').write_text(json.dumps(corpus,indent=2),encoding='utf-8')
        (output/'narrative-evaluation.json').write_text(json.dumps({'kind':'deterministic_validation_corpus','cases':evaluation,'passed':len(evaluation),'total':len(evaluation),'raw_spreadsheet_model_baseline':{'status':'not_run','reason':'Offline release verification does not call the provider. Use evaluate_live.py with a configured Groq key for the model comparison.','latency_ms':None,'token_use':None},'quality_improvement_claim':None},indent=2),encoding='utf-8')
        # 10,000 source rows, six exact budget grains, no repeated SQLite correction reads.
        def benchmark_ingest(wid):
            actual=io.StringIO(newline='');w=csv.writer(actual);w.writerow(['journal_id','line_id','date','department','account','amount','currency']);counts={}
            for i in range(10000):
                m=f'2026-{6+i%6:02d}';counts[m]=counts.get(m,0)+1;w.writerow([f'BENCH-{i}','1',m+'-15','Engineering','CLOUD','100.00','INR'])
            budget=io.StringIO(newline='');w=csv.writer(budget);w.writerow(['month','department','account','amount','budget_version','currency'])
            for m,n in counts.items():w.writerow([m,'Engineering','CLOUD',n*100,'FY27 Approved','INR'])
            load(wid,{'actuals':actual.getvalue().encode(),'budgets':budget.getvalue().encode()})
            result=validate(wid);assert result['blockers']==0
            sealed=publish(wid,'benchmark-runner')
            assert sealed['record_count']==10006
        timed('ingest_10000_cold',lambda:benchmark_ingest('benchmark-cold'),10000)
        timed('ingest_10000_warm',lambda:benchmark_ingest('benchmark-warm'),10000)
        # Confirm that edited current inputs block current exports while history stays frozen.
        upload=__import__('store').all_items('upload','demo')[0];upload['sheets'][0]['mapping']['scale']='thousands';put('upload',upload,'demo')
        assert client.get(f'/api/reports/{rid}/export/pdf').status_code==409
        assert client.get(f'/api/reports/{rid}/versions/{rev}/export/pdf?historical=true').content==pdf
        (output/'verification.json').write_text(json.dumps({'snapshot_id':sid,'report_id':rid,'approved_revision':rev,'payload_hash':hashed,'golden_variance_minor':18000000,'golden_unexplained_minor':1000000,'golden_scenario_closing_minor':330000000,'narrative_cases_passed':len(evaluation),'stale_export_blocked':True,'historical_export_identical':True,'generation_mode':'template','synthetic_only':True,'data_directory':str(run_dir)},indent=2),encoding='utf-8')
    (output/'timings.json').write_text(json.dumps({'clock':'time.perf_counter','environment':'local demo machine','includes':'10k ingestion includes parse, persistence, mapping, validation and Parquet publication; warm uses a second isolated workspace in the same process. Narrative timing uses template mode.','operations':timings},indent=2),encoding='utf-8')
    print('Synthetic release handoff verified:',output,flush=True)

if __name__=='__main__':main()
