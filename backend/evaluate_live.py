"""Optional, measured raw-spreadsheet versus constrained report comparison using Groq."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time
import uuid

def main():
    parser=argparse.ArgumentParser(description='Runs paid provider requests; requires an explicitly supplied --live flag.')
    parser.add_argument('--live',action='store_true');parser.add_argument('--release',default='artifacts/release');parser.add_argument('--cases',type=int,default=30)
    args=parser.parse_args()
    if not args.live:parser.error('Use --live to run the provider comparison, or release_verify.py for offline validation.')
    if not 1<=args.cases<=30:parser.error('--cases must be between 1 and 30.')
    release=Path(args.release).resolve();payload=json.loads((release/'approved-payload.json').read_text(encoding='utf-8'))['approved_payload'];packet=payload['packet']
    output=release/'live-evaluation'/uuid.uuid4().hex;output.mkdir(parents=True)
    os.environ['TRABO_DATA']=str(output/'data')
    from store import init
    import groq_adapter
    from reports import generation
    from narrative_cases import cases
    from report_content import validate_document,CAUSAL
    from parser import parse
    init()
    if not groq_adapter.status()['configured']:parser.error('GROQ_API_KEY is not configured. No requests were made.')
    verification=json.loads((release/'verification.json').read_text())
    data=Path(verification['data_directory']);raw=[];seen=set()
    for source in packet['evidence']:
        s=source['source']
        if s['file_hash'] in seen:continue
        seen.add(s['file_hash'])
        raw.append({'filename':s['filename'],'sheets':parse((data/'raw'/'demo'/s['file_hash']).read_bytes(),s['filename'])})
    results=[]
    for case in cases(packet)[:args.cases]:
        # Candidate notes are deliberately untrusted source context for both methods.
        note=json.dumps(case['document']);start=time.perf_counter()
        try:
            answer,usage=groq_adapter.raw_report_baseline({'period':packet['period'],'raw_spreadsheets':raw,'untrusted_candidate_note':note})
            expected={'period':packet['period'],'budget_minor':packet['totals']['budget_minor'],'actual_minor':packet['totals']['actual_minor'],'variance_minor':packet['totals']['variance_minor'],'unexplained_minor':packet['totals']['unexplained_minor']}
            omissions=[k for k in expected if k not in answer]
            invalid=[k for k,v in expected.items() if k in answer and answer[k]!=v]
            baseline={'answer':answer,'omissions':omissions,'invalid_claims':invalid,'unsupported_causal_text':bool(CAUSAL.search(str(answer.get('explanation','')))),'latency_ms':round((time.perf_counter()-start)*1000,2),'token_use':usage}
        except Exception as e:baseline={'error_type':type(e).__name__,'latency_ms':round((time.perf_counter()-start)*1000,2),'token_use':None}
        constrained=deepcopy(packet)
        constrained['owner_notes'].append({'id':'evaluation-'+case['label'],'snapshot_id':packet['snapshot_id'],'fact_id':packet['facts'][0]['id'],'owner':'Synthetic evaluation context','text':note,'evidence_state':'hypothesis','reviewed_by':'evaluation-runner'})
        document,log=generation('evaluation',constrained,'auto');checks=validate_document(document,constrained)
        results.append({'label':case['label'],'raw_spreadsheet_baseline':baseline,'constrained_workflow':{'valid':checks['valid'],'omissions':[e for e in checks['errors'] if e['code']=='omission'],'invalid_claims':checks['errors'],'latency_ms':log['latency_ms'],'token_use':log['token_use'],'mode':log['mode'],'attempts':log['attempts']}})
        (output/'results.json').write_text(json.dumps({'method':'same synthetic source files and deliberately untrusted candidate note; raw baseline calculates figures, constrained workflow binds stored facts and may fall back to templates','model':groq_adapter.status()['model'],'cases':results},indent=2),encoding='utf-8')
        print(case['label'],'recorded',flush=True)
    print('Measured provider comparison:',output/'results.json')

if __name__=='__main__':main()
