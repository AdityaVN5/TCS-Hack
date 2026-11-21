import json
import base64
import uuid
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
import reports
from report_models import ReportDocument
from report_exports import report_pdf, financial_csv
from store import get, all_items

class CreateReport(BaseModel):
    snapshot_id:str
    period:str|None=None
    scope:Literal['budget','mom','cumulative']='budget'
    scenario_id:str|None=None
    mode:Literal['auto','template']='auto'
    title:str=Field(default='Monthly management report',min_length=1,max_length=160)
    request_key:str=Field(default_factory=lambda:uuid.uuid4().hex,max_length=100)

class EditReport(BaseModel):
    expected_revision:int=Field(ge=1)
    document:ReportDocument

class Revision(BaseModel):
    expected_revision:int=Field(ge=1)

class Regenerate(CreateReport):
    expected_revision:int=Field(ge=1)

class Approval(Revision):
    expected_snapshot_id:str
    meaning_reviewed:bool=False
    unknowns_acknowledged:bool=False
    support_warnings_reviewed:bool=False
    assumptions_reviewed:bool=False
    comment:str=Field(min_length=3,max_length=2000)

def router_for(auth,wid):
    router=APIRouter(prefix='/api/reports',tags=['Phase 3 · Reviewed management reports'])
    def checked(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except reports.Conflict as e:raise HTTPException(409,str(e))
        except LookupError as e:raise HTTPException(404,str(e))
        except (ValueError,ValidationError) as e:raise HTTPException(422,str(e))

    @router.get('')
    def list_reports(w=Depends(wid)):return reports.list_reports(w)

    @router.get('/approved')
    def approved(w=Depends(wid)):return reports.approved_versions(w)

    @router.post('')
    def create(body:CreateReport,w=Depends(wid),user=Depends(auth)):
        return checked(reports.queue_report,w,user['email'],body.snapshot_id,body.period,body.scope,body.scenario_id,body.mode,body.title,request_key=body.request_key)

    @router.get('/status/{sid}')
    def snapshot_status(sid:str,w=Depends(wid)):
        if not get('snapshot',sid,w):raise HTTPException(404,'Snapshot not found in this workspace.')
        items=[r for r in reports.list_reports(w) if r['snapshot_id']==sid]
        return {'snapshot_id':sid,'report_status':next((r['state'] for r in items if r['state']=='approved'),items[0]['state'] if items else 'not_started'),'reports':items}

    @router.get('/{rid}')
    def read(rid:str,w=Depends(wid)):return checked(reports.read_report,w,rid)

    @router.get('/{rid}/versions/{revision}')
    def version(rid:str,revision:int,w=Depends(wid)):return checked(reports.read_report,w,rid,revision)

    @router.get('/{rid}/packet')
    def packet(rid:str,w=Depends(wid)):
        value=checked(reports.read_report,w,rid)
        if not value['version']:raise HTTPException(409,'The report is still generating.')
        return value['version']['packet']

    @router.get('/{rid}/evidence/{reference_id}')
    def evidence(rid:str,reference_id:str,revision:int|None=None,offset:int=0,limit:int=20,w=Depends(wid)):
        if offset<0 or not 1<=limit<=100:raise HTTPException(422,'Invalid evidence pagination.')
        from report_content import catalog
        value=checked(reports.read_report,w,rid,revision);version=value['version']
        if not version:raise HTTPException(409,'The draft is still generating.')
        packet=version['packet'];binding=catalog(packet).get(reference_id)
        if not binding:raise HTTPException(404,'Reference does not belong to this report packet.')
        fact=next((f for f in packet['facts'] if f['id']==reference_id),None)
        driver=next((d for d in packet['drivers'] if d['id']==reference_id),None)
        owner_note=next((n for n in packet['owner_notes'] if n['id']==reference_id),None)
        if owner_note:fact=next((f for f in packet['facts'] if f['id']==owner_note['fact_id']),None)
        ids=set((fact or driver or {}).get('source_ids',[]))
        calculation=driver or fact or packet['totals']
        if owner_note:calculation={'owner_note':owner_note,'fact':fact}
        if reference_id=='coverage':calculation=packet['coverage']
        if reference_id=='scope':calculation={k:packet[k] for k in ('snapshot_id','period','scope','budget_version','amount_unit','policy','calculation_versions')}
        if reference_id in ('summary','coverage','unknown_total','scope'):ids={r['id'] for r in packet['evidence'] if r['role'] in ('budgets','actuals','units_rates','headcount','invoices','allocations')}
        if binding['template'] in ('cash','assumptions'):
            ids={r['id'] for r in packet['evidence'] if r['role'] in ('cash','planned_positions','obligations')}
            calculation=packet['scenario']['result'] if packet['scenario'] else packet['baseline_cash']
        rows=[dict(r,original_url=f"/api/snapshots/{packet['snapshot_id']}/records/{r['id']}",download_url=f"/api/snapshots/{packet['snapshot_id']}/sources/{r['source']['file_hash']}") for r in packet['evidence'] if r['id'] in ids]
        return {'binding':binding,'calculation':calculation,'records':rows[offset:offset+limit],'total':len(rows),'offset':offset,'snapshot_id':packet['snapshot_id'],'packet_hash':version['packet_hash']}

    @router.put('/{rid}')
    def edit(rid:str,body:EditReport,w=Depends(wid),user=Depends(auth)):
        return checked(reports.edit_report,w,rid,body.expected_revision,body.document.model_dump(),user['email'])

    @router.post('/{rid}/regenerate')
    def regenerate(rid:str,body:Regenerate,w=Depends(wid),user=Depends(auth)):
        return checked(reports.queue_report,w,user['email'],body.snapshot_id,body.period,body.scope,body.scenario_id,body.mode,body.title,report_id=rid,expected_revision=body.expected_revision)

    @router.post('/{rid}/submit')
    def submit(rid:str,body:Revision,w=Depends(wid),user=Depends(auth)):
        return checked(reports.transition,w,rid,body.expected_revision,user['email'],'submit')

    @router.post('/{rid}/approve')
    def approve(rid:str,body:Approval,w=Depends(wid),user=Depends(auth)):
        attest=body.model_dump(include={'meaning_reviewed','unknowns_acknowledged','support_warnings_reviewed','assumptions_reviewed'})
        return checked(reports.transition,w,rid,body.expected_revision,user['email'],'approve',body.comment,attest,body.expected_snapshot_id)

    def export(rid,w,revision,historical,format,encoding='binary'):
        payload,hashed=checked(reports.approved_payload,w,rid,revision,historical)
        filename=f"TRABO-{payload['packet']['period']}-r{payload['revision']}{'-historical' if historical else ''}"
        headers={'Cache-Control':'private, no-store','X-Approved-Payload-SHA256':hashed,'X-Report-Revision':str(payload['revision'])}
        if format=='pdf':content=report_pdf(payload,hashed);media_type='application/pdf'
        elif format=='csv':content=financial_csv(payload,hashed);media_type='text/csv; charset=utf-8'
        else:content=json.dumps({'payload_hash':hashed,'approved_payload':payload},ensure_ascii=False,indent=2).encode();media_type='application/json'
        # A JSON envelope lets the UI validate errors and save exact bytes without
        # a browser PDF handler consuming the network response first.
        if encoding=='base64':return Response(json.dumps({'filename':f'{filename}.{format}','media_type':media_type,'content_base64':base64.b64encode(content).decode('ascii'),'payload_hash':hashed}),media_type='application/json',headers=headers)
        headers['Content-Disposition']=f'attachment; filename="{filename}.{format}"'
        return Response(content,media_type=media_type,headers=headers)

    @router.get('/{rid}/export/{format}')
    def current_export(rid:str,format:Literal['pdf','csv','json'],encoding:Literal['binary','base64']='binary',w=Depends(wid)):return export(rid,w,None,False,format,encoding)

    @router.get('/{rid}/versions/{revision}/export/{format}')
    def historical_export(rid:str,revision:int,format:Literal['pdf','csv','json'],historical:bool=False,encoding:Literal['binary','base64']='binary',w=Depends(wid)):return export(rid,w,revision,historical,format,encoding)
    return router
