import json
import math
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict
from analytics import build, records, VERSION
from scenarios import calculate, BUCKETS
from store import get, put, all_items, digest, now
import groq_adapter

def router_for(auth,wid):
    router=APIRouter(prefix='/api/phase2',tags=['Phase 2 · Financial analysis and scenarios'])
    def checked(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except ValueError as e:raise HTTPException(404 if 'not found' in str(e).lower() else 422,str(e))
    def artifact(kind,key,sid,w):
        value=get(kind,key,w)
        if not value or value['snapshot_id']!=sid:raise HTTPException(404,'Evidence does not belong to this workspace and snapshot.')
        return value

    @router.get('/provider')
    def provider(user=Depends(auth)):return groq_adapter.status()

    @router.post('/reference')
    def reference(w=Depends(wid),user=Depends(auth)):
        from reference import create_reference
        return checked(create_reference,w,user['email'])

    @router.get('/snapshots/{sid}/overview')
    def overview(sid:str,period:str|None=None,scope:Literal['budget','mom','cumulative']='budget',department:str|None=None,account:str|None=None,threshold_minor:int=2500000,percentage_threshold:float=10,executive_override_minor:int=10000000,jump_percentage:float=50,jump_amount_minor:int=2500000,credit_threshold_minor:int=2500000,w=Depends(wid)):
        if threshold_minor<1 or executive_override_minor<1 or not 0<=percentage_threshold<=10000:raise HTTPException(422,'Materiality thresholds must be positive.')
        if not math.isfinite(jump_percentage) or jump_percentage<0 or jump_percentage>10000 or jump_amount_minor<1 or credit_threshold_minor<1:raise HTTPException(422,'Review thresholds must be positive.')
        value=checked(build,w,sid,period,scope,department,account,threshold_minor,percentage_threshold,executive_override_minor,jump_percentage,jump_amount_minor,credit_threshold_minor)
        from reports import list_reports
        report_items=[r for r in list_reports(w) if r['snapshot_id']==sid]
        report_status=next((r['state'] for r in report_items if r['state']=='approved'),report_items[0]['state'] if report_items else 'not_started')
        return dict(value,owner_notes=[n for n in all_items('owner_note',w) if n['snapshot_id']==sid],report_review_status=report_status)

    @router.get('/snapshots/{sid}/evidence/{eid}')
    def evidence(sid:str,eid:str,offset:int=0,limit:int=20,w=Depends(wid)):
        if offset<0 or not 1<=limit<=100:raise HTTPException(422,'Invalid pagination.')
        item=get('driver',eid,w) or get('fact',eid,w) or get('alert',eid,w)
        if not item or item['snapshot_id']!=sid:raise HTTPException(404,'Evidence not found in this snapshot.')
        _,rows=checked(records,w,sid);ids=set(item['source_ids']);source=[r for r in rows if r['id'] in ids]
        # For a fact, include supporting records reachable through its disjoint drivers.
        for driver_id in item.get('driver_ids',[]):
            driver=artifact('driver',driver_id,sid,w);ids.update(driver['source_ids'])
        source=[dict(r,original_url=f"/api/snapshots/{sid}/records/{r['id']}",download_url=f"/api/snapshots/{sid}/sources/{r['source']['file_hash']}") for r in rows if r['id'] in ids]
        return {'snapshot_id':sid,'amount_unit':'INR paise','calculation_version':VERSION,'artifact':item,'offset':offset,'total':len(source),'records':source[offset:offset+limit],'notes':[n for n in all_items('owner_note',w) if n['snapshot_id']==sid and n['fact_id']==item.get('fact_id',eid)]}

    @router.get('/snapshots/{sid}/transactions')
    def transactions(sid:str,period:str|None=None,scope:Literal['budget','mom','cumulative']='budget',department:str|None=None,account:str|None=None,vendor:str|None=None,offset:int=0,limit:int=25,w=Depends(wid)):
        if offset<0 or not 1<=limit<=100:raise HTTPException(422,'Invalid pagination.')
        analysis=checked(build,w,sid,period,scope,department,account);_,rows=checked(records,w,sid);ids=set(analysis['transaction_ids'])
        rows=[r for r in rows if r['id'] in ids and (not vendor or r['data'].get('vendor')==vendor)]
        return {'snapshot_id':sid,'amount_unit':'INR paise','total':len(rows),'offset':offset,'records':rows[offset:offset+limit],'note':'Vendor filters apply only to posted transactions. Budgets remain at department/account grain.'}

    @router.post('/snapshots/{sid}/notes')
    def note(sid:str,body:OwnerNote,w=Depends(wid),user=Depends(auth)):
        artifact('fact',body.fact_id,sid,w)
        value={'id':digest([sid,body.model_dump(),now()]),'snapshot_id':sid,**body.model_dump(),'reviewed_by':user['email'],'reviewed_at':now(),'evidence_state':'owner_confirmed' if body.reviewed else 'hypothesis'}
        return put('owner_note',value,w)

    @router.post('/snapshots/{sid}/scenarios/calculate')
    def scenario_calculate(sid:str,body:ScenarioRequest,w=Depends(wid)):
        return checked(calculate,w,sid,body.parameters.model_dump(),body.assumptions.model_dump(exclude_none=True))

    @router.get('/snapshots/{sid}/scenarios/baseline')
    def baseline(sid:str,w=Depends(wid)):return checked(calculate,w,sid)

    @router.post('/snapshots/{sid}/scenarios')
    def save_scenario(sid:str,body:SaveScenario,w=Depends(wid),user=Depends(auth)):
        result=checked(calculate,w,sid,body.parameters.model_dump(),body.assumptions.model_dump(exclude_none=True))
        value={'id':digest([result['id'],body.name]),'snapshot_id':sid,'name':body.name,'result':result,'created_at':now(),'saved_by':user['email'],'parameters_hash':result['parameters_hash'],'assumptions_hash':result['assumptions_hash']}
        prior=get('scenario',value['id'],w)
        return prior or put('scenario',value,w)

    @router.get('/snapshots/{sid}/scenarios')
    def scenarios(sid:str,w=Depends(wid)):
        checked(records,w,sid)
        return [s for s in all_items('scenario',w) if s['snapshot_id']==sid]

    @router.get('/snapshots/{sid}/packet')
    def packet(sid:str,period:str|None=None,scope:Literal['budget','mom','cumulative']='budget',scenario_id:str|None=None,w=Depends(wid)):
        analysis=checked(build,w,sid,period,scope)
        scenario=artifact('scenario',scenario_id,sid,w) if scenario_id else None
        return {'contract_version':'trabo-evidence-packet-1','snapshot_id':sid,'amount_unit':'INR paise','period':analysis['period'],'scope':scope,'totals':analysis['totals'],'coverage':analysis['coverage'],'facts':analysis['facts'],'drivers':analysis['drivers'],'owner_notes':[n for n in all_items('owner_note',w) if n['snapshot_id']==sid],'scenario':scenario,'report_status':'ready_for_report_studio'}

    @router.post('/snapshots/{sid}/investigate')
    def investigate(sid:str,body:Question,w=Depends(wid)):
        fact=artifact('fact',body.fact_id,sid,w)
        if not groq_adapter.status()['configured']:raise HTTPException(503,'Set GROQ_API_KEY in the root .env file and restart the API to enable Groq guidance.')
        packet={'fact':fact,'drivers':[artifact('driver',d,sid,w) for d in fact['driver_ids']],'evidence_state':'unverified investigation guidance'}
        def events():
            yield json.dumps({'type':'status','mode':'groq','label':'AI guidance · unverified'})+'\n'
            try:
                for chunk in groq_adapter.stream_guidance(packet,body.question):yield json.dumps({'type':'delta','text':chunk})+'\n'
                yield json.dumps({'type':'done'})+'\n'
            except Exception:
                yield json.dumps({'type':'error','message':'Groq guidance is unavailable. Check your server-side key, model access, and connection. Verified financial results are unchanged.'})+'\n'
        return StreamingResponse(events(),media_type='application/x-ndjson',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
    return router

class OwnerNote(BaseModel):
    fact_id:str
    owner:str=Field(min_length=1,max_length=100)
    text:str=Field(min_length=3,max_length=3000)
    reviewed:bool=False

class Parameters(BaseModel):
    model_config=ConfigDict(extra='forbid')
    vendor_cut_bps:int=Field(default=0,ge=0,le=10000)
    vendor_effective_month:str
    freeze_position_ids:list[str]=Field(default_factory=list,max_length=1000)
    one_time_cost_minor:int=Field(default=0,ge=0,le=9000000000000000)
    one_time_month:str

class ScenarioRequest(BaseModel):
    parameters:Parameters
    assumptions:'Assumptions'=Field(default_factory=lambda:Assumptions())

class SaveScenario(ScenarioRequest):
    name:str=Field(min_length=1,max_length=100)

class Question(BaseModel):
    fact_id:str
    question:str=Field(default='Which source checks should I perform next?',min_length=3,max_length=1500)

class CashPeriod(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    month:str=Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
    opening_cash_minor:int
    receipts_minor:int=Field(ge=0)
    payments_minor:int=Field(ge=0)
    recurring_vendor_minor:int=Field(ge=0)
    existing_payroll_minor:int=Field(ge=0)
    other_operating_minor:int=Field(ge=0)
    taxes_minor:int=Field(ge=0)
    capex_minor:int=Field(ge=0)
    debt_service_minor:int=Field(ge=0)
    scheduled_invoices_minor:int=Field(ge=0)
    planned_hires_minor:int=Field(ge=0)
    breakdown_reviewed:bool=False
    vendor_cancellable:bool=False

class Assumptions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reason:str=Field(default='',max_length=2000)
    periods:list[CashPeriod]|None=Field(default=None,min_length=12,max_length=12)

ScenarioRequest.model_rebuild()
SaveScenario.model_rebuild()
