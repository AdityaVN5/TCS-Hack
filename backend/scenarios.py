"""Twelve-month cash model. Bucket exclusions and timing precede savings."""
import calendar
from copy import deepcopy
from datetime import date
from decimal import Decimal
from analytics import records, rounded
from store import digest

VERSION='trabo-scenario-2.1'
BUCKETS=('recurring_vendor','existing_payroll','other_operating','taxes','capex','debt_service','scheduled_invoices','planned_hires')

def months_after(period):
    y,m=map(int,period.split('-'));start=y*12+m
    return [f'{n//12:04d}-{n%12+1:02d}' for n in range(start,start+12)]

def position_cost(position,month):
    start=date.fromisoformat(position['data']['start_date']);y,m=map(int,month.split('-'));days=calendar.monthrange(y,m)[1]
    first=date(y,m,1);last=date(y,m,days)
    if start>last:return 0
    eligible=(last-max(first,start)).days+1
    return rounded(Decimal(position['amount'])*eligible/days)

def calculate(wid,sid,parameters=None,assumptions=None):
    manifest,source=records(wid,sid);periods=months_after(manifest['policy']['period'])
    parameters=parameters or {};assumptions=assumptions or {}
    params={'vendor_cut_bps':0,'vendor_effective_month':periods[1],'freeze_position_ids':[],'one_time_cost_minor':0,'one_time_month':periods[0],**parameters}
    if not 0<=params['vendor_cut_bps']<=10000:raise ValueError('Vendor reduction must be between 0% and 100%.')
    if params['vendor_effective_month'] not in periods or params['one_time_month'] not in periods:raise ValueError('Action dates must be within the twelve-month horizon.')
    if params['one_time_cost_minor']<0:raise ValueError('One-time costs cannot be negative.')
    params['freeze_position_ids']=sorted(set(params['freeze_position_ids']))
    positions=[r for r in source if r['role']=='planned_positions'];ids=[r['data']['position_id'] for r in positions]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate planned-position IDs must be resolved before modeling hires.')
    if set(params['freeze_position_ids'])-set(ids):raise ValueError('Unknown planned position in freeze selection.')
    exclusions=[];position_list=[]
    for p in positions:
        d=p['data'];committed=str(d['committed']).lower()=='true';started=d['start_date'][:7]<=manifest['policy']['period'];eligible=not committed and not started
        item={'id':d['position_id'],'department':d['department'],'start_date':d['start_date'],'monthly_cost_minor':p['amount'],'eligible':eligible,'reason':'Signed commitment' if committed else 'Position already started' if started else 'Uncommitted future position','source_id':p['id']}
        position_list.append(item)
        if not eligible:
            exclusions.append(item)
            if d['position_id'] in params['freeze_position_ids']:raise ValueError(f"{d['position_id']} cannot be frozen: {item['reason']}.")
    cash=[r for r in source if r['role']=='cash'];by_month={r['data']['month']:r for r in cash}
    if len(by_month)!=len(cash):raise ValueError('Duplicate cash periods must be resolved before modeling cash.')
    overrides=assumptions.get('periods')
    notes=[];reason=assumptions.get('reason','')
    if overrides is not None:
        if len(overrides)!=12 or {r['month'] for r in overrides}!=set(periods):raise ValueError('Assumptions must cover each of the twelve forward months exactly once.')
        if not str(reason).strip():raise ValueError('Document the reason for reviewed cash assumptions.')
        by_month={r['month']:{'id':'assumption:'+r['month'],'data':r} for r in overrides}
    complete=all(m in by_month for m in periods)
    if not complete:notes.append('Cash assumptions are absent or incomplete. Only expense savings are shown; runway is unavailable.')
    cash_valid=complete
    opening=None;raw_periods=[]
    for m in periods:
        record=by_month.get(m);d=record['data'] if record else {}
        valid=all(k+'_minor' in d for k in ('opening_cash','receipts','payments'))
        reviewed=str(d.get('breakdown_reviewed','')).lower()=='true'
        buckets={k:int(d.get(k+'_minor',0)) for k in BUCKETS}
        has_buckets=all(k+'_minor' in d for k in BUCKETS)
        if valid and any(int(d[k+'_minor'])<0 for k in ('receipts','payments')):raise ValueError('Cash receipts and payments must be nonnegative.')
        if any(v<0 for v in buckets.values()):raise ValueError('Cash payment buckets must be nonnegative.')
        if valid and has_buckets and sum(buckets.values())!=int(d['payments_minor']):raise ValueError(f'{m}: exclusive payment buckets must sum to total cash payments.')
        if not has_buckets:
            # Legacy aggregate cash can support baseline runway, but cannot imply eligible vendor spend.
            buckets['other_operating']=int(d.get('payments_minor',0))
        if valid and opening is None:opening=int(d['opening_cash_minor'])
        cash_valid=cash_valid and valid
        raw_periods.append({'month':m,'source_id':record and record['id'],'opening_cash_minor':d.get('opening_cash_minor'),'receipts_minor':int(d['receipts_minor']) if valid else None,'payments_minor':int(d['payments_minor']) if valid else None,'buckets':buckets,'eligible_vendor_minor':buckets['recurring_vendor'] if has_buckets and reviewed and str(d.get('vendor_cancellable','')).lower()=='true' else 0,'has_reviewed_buckets':has_buckets and reviewed})
    seen_obligations=set();seen_invoices=set();scheduled_totals={}
    for obligation in [r for r in source if r['role']=='obligations']:
        d=obligation['data'];ref=d.get('invoice_id');oid=d['obligation_id']
        if oid in seen_obligations or (ref and ref in seen_invoices):
            exclusions.append({'id':oid,'reason':'Duplicate obligation / invoice reference; excluded from commitment support','amount_minor':obligation['amount']});continue
        seen_obligations.add(oid)
        if ref:seen_invoices.add(ref)
        if str(d['committed']).lower()=='true':
            exclusions.append({'id':oid,'reason':'Scheduled committed payment; never cut or added again','amount_minor':obligation['amount'],'source_id':obligation['id']})
            row=next((r for r in raw_periods if r['month']==d['date'][:7]),None)
            if row:
                if d['category']=='scheduled_invoice':
                    scheduled_totals[row['month']]=scheduled_totals.get(row['month'],0)+max(0,obligation['amount'])
                    if row['has_reviewed_buckets'] and scheduled_totals[row['month']]>row['buckets']['scheduled_invoices']:raise ValueError('Scheduled commitments exceed their exclusive cash bucket.')
                else:
                    # Legacy commitments with uncertain bucket membership conservatively reduce eligibility.
                    row['eligible_vendor_minor']=max(0,row['eligible_vendor_minor']-max(0,obligation['amount']))
    if params['vendor_cut_bps'] and not any(r['eligible_vendor_minor'] for r in raw_periods):raise ValueError('No reviewed cancellable vendor base is available. Review explicit payment buckets first.')
    assumption_periods=deepcopy(raw_periods)
    baseline=[];scenario=[];bclose=opening if cash_valid else None;sclose=bclose;vendor_total=hire_total=cost_total=0
    for row in raw_periods:
        m=row['month'];buckets=row['buckets'];all_hires=sum(position_cost(p,m) for p in positions);hire=sum(position_cost(p,m) for p in positions if p['data']['position_id'] in params['freeze_position_ids'])
        if row['has_reviewed_buckets'] and all_hires>buckets['planned_hires']:raise ValueError(f'{m}: planned positions exceed their exclusive cash payment bucket.')
        if hire and cash_valid and not row['has_reviewed_buckets']:raise ValueError('Review the planned-hire payment bucket before modeling cash savings from a freeze.')
        vendor=rounded(Decimal(row['eligible_vendor_minor'])*params['vendor_cut_bps']/10000) if m>=params['vendor_effective_month'] else 0
        cost=params['one_time_cost_minor'] if m==params['one_time_month'] else 0
        receipts=row['receipts_minor'] if cash_valid else None;payments=row['payments_minor'] if cash_valid else None
        if cash_valid:
            # Source opening figures are continuity controls, not independent monthly resets.
            if int(row['opening_cash_minor'])!=bclose:raise ValueError(f'{m}: opening cash does not reconcile with the prior baseline closing cash.')
            base_open=bclose;scenario_open=sclose;bclose+=receipts-payments;sclose+=receipts-payments+vendor+hire-cost
            baseline.append({'month':m,'opening_minor':base_open,'receipts_minor':receipts,'payments_minor':payments,'closing_minor':bclose})
            scenario.append({'month':m,'opening_minor':scenario_open,'receipts_minor':receipts,'payments_minor':payments-vendor-hire+cost,'closing_minor':sclose})
        else:
            baseline.append({'month':m,'opening_minor':None,'receipts_minor':None,'payments_minor':None,'closing_minor':None})
            scenario.append(dict(baseline[-1]))
        row.update(vendor_saving_minor=vendor,hiring_saving_minor=hire,one_time_cost_minor=cost,net_saving_minor=vendor+hire-cost)
        vendor_total+=vendor;hire_total+=hire;cost_total+=cost
    assumptions_payload={'periods':assumption_periods,'reason':reason,'cash_available':cash_valid,'opening_cash_minor':opening if cash_valid else None,'source_snapshot':sid,'version':VERSION}
    assumption_hash=digest(assumptions_payload);parameter_hash=digest(params)
    first_negative=lambda values:next((r['month'] for r in values if r['closing_minor'] is not None and r['closing_minor']<0),None)
    return {'id':digest([sid,VERSION,assumption_hash,parameter_hash]),'snapshot_id':sid,'amount_unit':'INR paise','calculation_version':VERSION,'parameters':params,'assumptions_hash':assumption_hash,'parameters_hash':parameter_hash,'assumptions':assumptions_payload,'input_assumptions':assumptions,'periods':periods,'cash_available':cash_valid,'notes':notes,'baseline':baseline,'scenario':scenario,'payment_schedule':raw_periods,'positions':position_list,'excluded_commitments':exclusions,'summary':{'baseline_closing_minor':bclose,'scenario_closing_minor':sclose,'vendor_savings_minor':vendor_total,'hiring_savings_minor':hire_total,'one_time_cost_minor':cost_total,'net_savings_minor':vendor_total+hire_total-cost_total,'baseline_first_negative_month':first_negative(baseline),'scenario_first_negative_month':first_negative(scenario)},'transformation_order':['review exclusive buckets','exclude commitments and duplicate references','apply eligible vendor reduction from effective month','freeze selected uncommitted hires with calendar-day proration','apply one-time cost','roll cash forward']}
