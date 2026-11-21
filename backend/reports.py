"""Versioned reports, content-bound review, immutable approvals, and freshness checks."""
from copy import deepcopy
import json
import os
import time
import uuid
from analytics import build, records, VERSION as ANALYSIS_VERSION
from scenarios import calculate, VERSION as SCENARIO_VERSION
from engine import input_fingerprint
from store import db, get, put, all_items, digest, now
from report_models import ReportDocument
from report_content import catalog, template_document, validate_document, render_document, GENERATION_VERSION, PROMPT_VERSION, VERSION as RENDER_VERSION
import groq_adapter

class Conflict(ValueError):pass

def require(kind,key,wid):
    value=get(kind,key,wid)
    if not value:raise LookupError('Record not found in this workspace.')
    return value

def relevant_notes(wid,sid,fact_ids):
    return sorted([n for n in all_items('owner_note',wid) if n['snapshot_id']==sid and n['fact_id'] in fact_ids],key=lambda n:n['id'])

def collect_packet(wid,sid,period=None,scope='budget',scenario_id=None):
    manifest,source=records(wid,sid);analysis=build(wid,sid,period,scope)
    scenario=require('scenario',scenario_id,wid) if scenario_id else None
    if scenario and scenario['snapshot_id']!=sid:raise ValueError('The selected scenario belongs to another snapshot.')
    baseline=calculate(wid,sid)
    material=[f['id'] for f in analysis['facts'] if f['material']][:20]
    if not material:material=[f['id'] for f in analysis['facts'][:4]]
    selected=set(material)
    drivers=[d for d in analysis['drivers'] if d['fact_id'] in selected]
    needed={rid for f in analysis['facts'] for rid in f['source_ids']}
    needed.update(rid for d in drivers for rid in d['source_ids'])
    needed.update(r['id'] for r in source if r['role'] in ('cash','planned_positions','obligations'))
    evidence=[{'id':r['id'],'role':r['role'],'source':r['source']} for r in source if r['id'] in needed]
    known={r['id'] for r in source}
    if not needed.issubset(known):raise ValueError('A calculation references a source outside the sealed snapshot.')
    packet={'contract_version':'trabo-report-packet-1','snapshot_id':sid,'company':manifest['policy']['name'],'entity':manifest['policy']['entity'],'period':analysis['period'],'scope':scope,'amount_unit':'INR paise','budget_version':manifest['policy']['budget_version'],'policy':manifest['policy'],'totals':analysis['totals'],'coverage':analysis['coverage'],'facts':analysis['facts'],'material_fact_ids':material,'drivers':drivers,'owner_notes':relevant_notes(wid,sid,{f['id'] for f in analysis['facts']}),'scenario':scenario,'baseline_cash':baseline,'support_warnings':manifest.get('warnings',[]),'evidence':evidence,'calculation_versions':{'analysis':ANALYSIS_VERSION,'scenario':SCENARIO_VERSION,'report':RENDER_VERSION},'reference':manifest.get('reference',False)}
    return packet

def context_for(wid,packet):
    return {'draft_fingerprint':None if packet['reference'] else input_fingerprint(wid),'notes_hash':digest(packet['owner_notes']),'scenario_hash':digest(packet['scenario']) if packet['scenario'] else None,'packet_hash':digest(packet)}

def freshness(wid,version):
    packet=version['packet'];context=version['source_context'];reasons=[]
    manifest=get('snapshot',packet['snapshot_id'],wid)
    if not manifest:return ['The selected source snapshot is unavailable.']
    candidates=[s for s in all_items('snapshot',wid) if s['policy']['entity']==packet['entity'] and bool(s.get('reference'))==bool(packet['reference'])]
    if candidates and max(candidates,key=lambda s:(s['created_at'],s['id']))['id']!=packet['snapshot_id']:reasons.append('A newer reconciled snapshot is available for this entity.')
    if not packet['reference']:
        current=input_fingerprint(wid)
        current_policy=get('workspace',wid,wid)
        if current_policy and digest(current_policy)!=digest(manifest['policy']):reasons.append('Workspace accounting policies differ from the selected sealed version.')
        if current!=context['draft_fingerprint']:reasons.append('Source files, mappings, corrections, or workspace policies changed after drafting.')
        if all_items('upload',wid) and current!=packet['snapshot_id']:reasons.append('Current draft inputs are not the selected reconciled snapshot.')
        validation=get('validation','current',wid)
        if validation and validation['blockers']:reasons.append('The current close has unresolved blocking issues.')
    notes=relevant_notes(wid,packet['snapshot_id'],{f['id'] for f in packet['facts']})
    if digest(notes)!=context['notes_hash']:reasons.append('Included owner context changed after drafting.')
    if packet['scenario']:
        saved=get('scenario',packet['scenario']['id'],wid)
        if not saved or digest(saved)!=context['scenario_hash']:reasons.append('The selected saved scenario or its included assumptions changed.')
    return reasons

def generation(wid,packet,mode='auto'):
    started=time.perf_counter();packet_hash=digest(packet);configured=groq_adapter.status()['configured']
    provider_model=groq_adapter.status()['model'];config={'model':provider_model,'temperature':os.getenv('GROQ_REPORT_TEMPERATURE','0'),'max_tokens':os.getenv('GROQ_REPORT_MAX_COMPLETION_TOKENS','8192'),'reasoning':os.getenv('GROQ_REASONING_EFFORT','medium')}
    cache_key=digest([packet_hash,PROMPT_VERSION,GENERATION_VERSION,config])
    log={'id':uuid.uuid4().hex,'packet_hash':packet_hash,'prompt_version':PROMPT_VERSION,'generation_version':GENERATION_VERSION,'render_version':RENDER_VERSION,'model':provider_model,'created_at':now(),'attempts':[],'token_use':{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0},'mode':'template','reason':'requested_template' if mode=='template' else 'provider_not_configured'}
    cached=get('report_generation_cache',cache_key,wid) if mode!='template' and configured else None
    if cached and validate_document(cached['document'],packet)['valid']:
        document=deepcopy(cached['document']);log.update(mode='cached',reason='exact_packet_and_generation_versions',cache_source=cached['generation_id'],original_token_use=cached['token_use'])
    elif mode!='template' and configured:
        repair=None;document=None
        provider_packet={'scope':{'snapshot_id':packet['snapshot_id'],'period':packet['period'],'amount_unit':'INR paise','budget_version':packet['budget_version']},'reference_catalog':list(catalog(packet).values()),'required_template_document':template_document(packet),'owner_context_untrusted':packet['owner_notes']}
        for attempt in range(2):
            request_started=time.perf_counter()
            try:
                candidate,usage=groq_adapter.generate_report_json(provider_packet,ReportDocument.model_json_schema(),repair)
                if usage:
                    for key in log['token_use']:log['token_use'][key]+=usage.get(key,0) or 0
                validation=validate_document(candidate,packet)
                log['attempts'].append({'attempt':attempt+1,'valid':validation['valid'],'errors':validation['errors'],'usage':usage,'latency_ms':round((time.perf_counter()-request_started)*1000,2)})
                if validation['valid']:
                    document=ReportDocument.model_validate(candidate).model_dump();log.update(mode='fresh',reason='validated_provider_output');break
                repair=validation['errors'];log['reason']='provider_validation_failed'
            except Exception as e:
                log['attempts'].append({'attempt':attempt+1,'valid':False,'error_type':type(e).__name__,'latency_ms':round((time.perf_counter()-request_started)*1000,2)})
                log['reason']='provider_unavailable';break
        if document is not None:put('report_generation_cache',{'id':cache_key,'document':document,'generation_id':log['id'],'token_use':deepcopy(log['token_use'])},wid)
        else:document=template_document(packet)
    else:document=template_document(packet)
    validation=validate_document(document,packet)
    if not validation['valid']:raise ValueError('The deterministic report template did not pass its evidence checks.')
    log.update(validation=validation,latency_ms=round((time.perf_counter()-started)*1000,2))
    put('report_generation',log,wid)
    return document,log

def queue_report(wid,actor,sid,period=None,scope='budget',scenario_id=None,mode='auto',title='Monthly management report',report_id=None,expected_revision=None,request_key=None):
    if request_key and not report_id:
        request=get('report_request',digest([actor,request_key]),wid)
        if request:return read_report(wid,request['report_id'])
    packet=collect_packet(wid,sid,period,scope,scenario_id);source_context=context_for(wid,packet)
    if report_id:
        header=require('report',report_id,wid)
        if header['revision']!=expected_revision or header.get('pending'):raise Conflict('The report changed. Reload before creating another revision.')
    else:
        report_id=uuid.uuid4().hex
        header={'id':report_id,'title':title,'created_at':now(),'created_by':actor,'revision':0,'head_version_id':None,'history':[]}
    pending={'packet':packet,'source_context':source_context,'mode':mode,'actor':actor,'queued_at':now(),'base_revision':header['revision']}
    # Guard the exact head while attaching generation work.
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT body FROM objects WHERE kind='report' AND id=? AND workspace=?",(report_id,wid)).fetchone()
        if row:
            current=json.loads(row['body'])
            if current['revision']!=header['revision'] or current.get('pending'):raise Conflict('The report changed. Reload before regenerating.')
            header=current
        header['pending']=pending;header['error']=None
        c.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',('report',report_id,wid,json.dumps(header)))
    from worker import enqueue
    job=enqueue(wid,'report',actor,{'report_id':report_id,'base_revision':header['revision']})
    if request_key:put('report_request',{'id':digest([actor,request_key]),'report_id':report_id},wid)
    value=read_report(wid,report_id);value['job_id']=job['id'];return value

def complete_generation(wid,report_id):
    header=require('report',report_id,wid);pending=header.get('pending')
    if not pending:
        if header['head_version_id']:return require('report_version',header['head_version_id'],wid)
        raise ValueError('Report generation has no pending packet.')
    document,log=generation(wid,pending['packet'],pending['mode'])
    rev=pending['base_revision']+1
    version={'id':f'{report_id}:{rev}','report_id':report_id,'revision':rev,'snapshot_id':pending['packet']['snapshot_id'],'packet':pending['packet'],'packet_hash':digest(pending['packet']),'source_context':pending['source_context'],'document':document,'generation':log,'created_at':now(),'created_by':pending['actor'],'state':'draft','validation':validate_document(document,pending['packet']),'decisions':[]}
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        current=json.loads(c.execute("SELECT body FROM objects WHERE kind='report' AND id=? AND workspace=?",(report_id,wid)).fetchone()['body'])
        if current['revision']!=pending['base_revision']:
            return require('report_version',current['head_version_id'],wid)
        c.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',('report_version',version['id'],wid,json.dumps(version)))
        current.update(revision=rev,head_version_id=version['id'],pending=None,error=None)
        current['history'].append(version['id'])
        c.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',('report',report_id,wid,json.dumps(current)))
    return version

def decorate(wid,header,version):
    value=deepcopy(version);reasons=freshness(wid,version)
    value['stale_reasons']=reasons;value['effective_state']='stale' if reasons else version['state']
    value['historical']=header['head_version_id']!=version['id'] or bool(reasons)
    value['validation']=validate_document(version['document'],version['packet'])
    value['rendered']=render_document(version['document'],version['packet'])
    value['claim_catalog']=list(catalog(version['packet']).values())
    return value

def read_report(wid,report_id,revision=None):
    header=require('report',report_id,wid)
    key=f'{report_id}:{revision}' if revision else header['head_version_id']
    value=deepcopy(header);value.pop('pending',None);value['generating']=bool(header.get('pending'))
    value['generation_job']=None
    if header.get('pending'):
        jobs=[j for j in all_items('job',wid) if j['kind']=='report' and j.get('payload',{}).get('report_id')==report_id and j.get('payload',{}).get('base_revision')==header['pending']['base_revision']]
        if jobs:
            job=jobs[-1]
            value['generation_job']={k:job.get(k) for k in ('id','status','error','attempts')}
    value['version']=decorate(wid,header,require('report_version',key,wid)) if key else None
    value['versions']=[]
    for key in reversed(header['history']):
        v=require('report_version',key,wid)
        value['versions'].append({'revision':v['revision'],'id':v['id'],'state':v['state'],'snapshot_id':v['snapshot_id'],'created_at':v['created_at'],'approval':v.get('approval'),'historical':key!=header['head_version_id']})
    return value

def list_reports(wid):
    values=[]
    for r in reversed(all_items('report',wid)):
        value=read_report(wid,r['id']);v=value['version']
        failed=value['generation_job'] and value['generation_job']['status']=='failed'
        values.append({'id':r['id'],'title':r['title'],'revision':r['revision'],'created_at':r['created_at'],'generating':value['generating'],'state':'generation_failed' if failed else v['effective_state'] if v else 'generating','period':v['packet']['period'] if v else r['pending']['packet']['period'],'snapshot_id':v['snapshot_id'] if v else r['pending']['packet']['snapshot_id'],'approval':v.get('approval') if v else None})
    return values

def approved_versions(wid):
    items=[]
    for header in all_items('report',wid):
        for vid in header['history']:
            v=require('report_version',vid,wid)
            if v['state']!='approved':continue
            reasons=freshness(wid,v)
            items.append({'report_id':header['id'],'title':header['title'],'revision':v['revision'],'snapshot_id':v['snapshot_id'],'period':v['packet']['period'],'approval':v['approval'],'current':vid==header['head_version_id'] and not reasons,'stale_reasons':reasons,'payload_hash':v['payload_hash']})
    return sorted(items,key=lambda i:i['approval']['approved_at'],reverse=True)

def edit_report(wid,report_id,expected_revision,document,actor):
    header=require('report',report_id,wid)
    if header['revision']!=expected_revision or header.get('pending'):raise Conflict('A newer report revision exists. Reload before saving.')
    previous=require('report_version',header['head_version_id'],wid)
    # Even invalid numerical edits are retained as drafts, never written back into facts.
    raw=ReportDocument.model_validate(document).model_dump();rev=expected_revision+1
    version={k:deepcopy(v) for k,v in previous.items() if k not in ('approval','approved_payload','payload_hash','review_submitted_at','review_submitted_by')}
    version.update(id=f'{report_id}:{rev}',revision=rev,document=raw,validation=validate_document(raw,version['packet']),state='draft',created_at=now(),created_by=actor,decisions=[])
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        current=json.loads(c.execute("SELECT body FROM objects WHERE kind='report' AND id=? AND workspace=?",(report_id,wid)).fetchone()['body'])
        if current['revision']!=expected_revision or current.get('pending'):raise Conflict('A newer report revision exists. Reload before saving.')
        c.execute('INSERT INTO objects VALUES(?,?,?,?)',('report_version',version['id'],wid,json.dumps(version)))
        current.update(revision=rev,head_version_id=version['id']);current['history'].append(version['id'])
        c.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',('report',report_id,wid,json.dumps(current)))
    return read_report(wid,report_id)

def transition(wid,report_id,expected_revision,actor,action,comment='',attestations=None,expected_snapshot=None):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT body FROM objects WHERE kind='report' AND id=? AND workspace=?",(report_id,wid)).fetchone()
        if not row:raise LookupError('Report not found in this workspace.')
        header=json.loads(row['body'])
        if header['revision']!=expected_revision or header.get('pending'):raise Conflict('The exact report head changed. Reload before reviewing.')
        version=require('report_version',header['head_version_id'],wid)
        checks=validate_document(version['document'],version['packet']);reasons=freshness(wid,version)
        if reasons:raise Conflict('This report is stale: '+' '.join(reasons))
        if not checks['valid'] or checks['traceability_percentage']!=100:raise Conflict('Fix all report validation errors before review or approval.')
        if action=='submit':
            if version['state']!='draft':raise Conflict('Only a draft can be submitted for review.')
            version.update(state='in_review',review_submitted_at=now(),review_submitted_by=actor)
        elif action=='approve':
            if version['state']!='in_review':raise Conflict('Submit this exact revision for review before approval.')
            if expected_snapshot!=version['snapshot_id']:raise Conflict('The snapshot selected for approval changed.')
            if not attestations or not all(attestations.get(k) is True for k in ('meaning_reviewed','unknowns_acknowledged','support_warnings_reviewed','assumptions_reviewed')):raise ValueError('Explicitly review meaning, unknowns, support warnings, and included assumptions.')
            if checks['unassigned_actions']:raise ValueError('Assign an owner to each proposed action before approval.')
            if len(comment.strip())<3:raise ValueError('Record a review decision of at least three characters.')
            approval={'reviewer':actor,'approved_at':now(),'report_id':report_id,'revision':version['revision'],'snapshot_id':version['snapshot_id'],'packet_hash':version['packet_hash'],'comment':comment,'attestations':attestations}
            payload={'report_id':report_id,'revision':version['revision'],'title':header['title'],'rendered':render_document(version['document'],version['packet']),'packet':deepcopy(version['packet']),'document':deepcopy(version['document']),'generation':version['generation'],'validation':checks,'approval':approval,'synthetic':version['packet']['reference']}
            version.update(state='approved',approval=approval,approved_payload=payload,payload_hash=digest(payload))
        else:raise ValueError('Unknown report transition.')
        version['decisions'].append({'action':action,'actor':actor,'timestamp':now(),'comment':comment,'revision':expected_revision})
        c.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',('report_version',version['id'],wid,json.dumps(version)))
    return read_report(wid,report_id)

def approved_payload(wid,report_id,revision=None,historical=False):
    header=require('report',report_id,wid);key=f'{report_id}:{revision}' if revision else header['head_version_id']
    version=require('report_version',key,wid)
    if version['state']!='approved' or 'approved_payload' not in version:raise Conflict('Only a frozen approved revision can be exported.')
    if (key!=header['head_version_id'] or freshness(wid,version)) and not historical:raise Conflict('This approval is historical. Explicitly select its historical version to download it.')
    if digest(version['approved_payload'])!=version['payload_hash']:raise Conflict('The frozen approval payload failed its integrity check.')
    return version['approved_payload'],version['payload_hash']
