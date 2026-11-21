"""Snapshot-scoped exact variances. All monetary outputs are integer INR paise."""
import json
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
import duckdb
import store
from store import get, put, digest
from engine import reporting_months

VERSION='trabo-analysis-2.1'
ACCOUNT_NAMES={'CLOUD':'Cloud infrastructure','PAYROLL':'Payroll','MARKETING':'Marketing campaigns','OTHER':'Other operating costs'}
STATES={'supported','owner_confirmed'}

def rounded(number):return int(Decimal(number).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
def percentage(amount,base):return float((Decimal(amount)*100/abs(base)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) if base else None

def records(wid,sid):
    manifest=get('snapshot',sid,wid)
    if not manifest:raise ValueError('Snapshot not found in this workspace.')
    con=duckdb.connect()
    try:
        rows=con.execute('SELECT payload FROM read_parquet(?) ORDER BY record_id',[str(store.ROOT/'snapshots'/wid/sid/'canonical.parquet')]).fetchall()
    finally:con.close()
    return manifest,[json.loads(r[0]) for r in rows]

def build(wid,sid,period=None,scope='budget',department=None,account=None,threshold=2500000,percent_threshold=10,override=10000000,jump_percentage=50,jump_amount=2500000,credit_threshold=2500000):
    manifest=get('snapshot',sid,wid)
    if not manifest:raise ValueError('Snapshot not found in this workspace.')
    periods=reporting_months(manifest['policy']['period']);period=period or periods[-1]
    if period not in periods:raise ValueError('Choose one of the snapshot’s six historical months.')
    if scope not in ('budget','mom','cumulative'):raise ValueError('Unknown comparison scope.')
    if scope=='mom' and period==periods[0]:raise ValueError('No prior complete month exists in this snapshot.')
    policy={'threshold_minor':threshold,'percentage_threshold':percent_threshold,'executive_override_minor':override,'jump_percentage':jump_percentage,'jump_amount_minor':jump_amount,'credit_threshold_minor':credit_threshold}
    aid=digest([VERSION,sid,period,scope,department,account,policy])
    cached=get('analysis',aid,wid)
    if cached:return cached
    _,rows=records(wid,sid)
    active=periods[:periods.index(period)+1] if scope=='cumulative' else [period]
    prior=periods[periods.index(period)-1] if scope=='mom' else None
    selected=[r for r in rows if (not department or r['data'].get('department')==department) and (not account or r['data'].get('account')==account)]
    actual=[r for r in selected if r['role']=='actuals' and r['data']['month'] in active]
    budget=[r for r in selected if r['role']==('actuals' if scope=='mom' else 'budgets') and r['data']['month'] in ([prior] if prior else active)]
    groups=defaultdict(lambda:{'actual':[],'budget':[]})
    for kind,items in [('actual',actual),('budget',budget)]:
        for r in items:groups[(r['data']['department'],r['data']['account'])][kind].append(r)
    facts=[];drivers=[]
    def make_driver(fact,label,amount,state,formula,inputs,evidence,kind):
        if not amount:return
        item={'id':digest([VERSION,fact['id'],kind,label,len(fact['driver_ids'])]),'snapshot_id':sid,'fact_id':fact['id'],'period':period,'scope':scope,'label':label,'kind':kind,'amount_minor':amount,'evidence_state':state,'formula':formula,'inputs':inputs,'source_ids':sorted(set(evidence)),'calculation_version':VERSION,'amount_unit':'INR paise'}
        put('driver',item,wid);drivers.append(item);fact['driver_ids'].append(item['id'])
    for (dep,acc),items in sorted(groups.items()):
        a=sum(r['amount'] for r in items['actual']);b=sum(r['amount'] for r in items['budget']);var=a-b
        fact={'id':digest([VERSION,sid,period,scope,dep,acc]),'snapshot_id':sid,'period':period,'scope':scope,'department':dep,'account':acc,'label':ACCOUNT_NAMES.get(acc,acc),'actual_minor':a,'budget_minor':b,'variance_minor':var,'percentage':percentage(var,b),'unbudgeted':b==0,'direction':'adverse' if var>0 else 'favorable' if var<0 else 'neutral','material':abs(var)>=override or (abs(var)>=threshold and (b==0 or abs(Decimal(var))*100>=abs(b)*Decimal(str(percent_threshold)))),'source_ids':sorted(r['id'] for r in items['actual']+items['budget']),'driver_ids':[],'amount_unit':'INR paise','calculation_version':VERSION,'formula':'sum(actual_minor) - sum(comparison_minor)'}
        if scope=='mom':
            make_driver(fact,'Month-on-month movement',var,'unknown','current actual - prior-month actual',{'current_minor':a,'prior_minor':b},fact['source_ids'],'residual')
        else:
            for month in active:
                ar=[r for r in items['actual'] if r['data']['month']==month];br=[r for r in items['budget'] if r['data']['month']==month]
                ma=sum(r['amount'] for r in ar);mb=sum(r['amount'] for r in br);remaining=ma-mb
                source_ids=[r['id'] for r in ar+br]
                support=[r for r in rows if r['data'].get('month')==month and r['data'].get('department')==dep and (r['role']=='headcount' if acc=='PAYROLL' else r['role']=='units_rates' and r['data'].get('account')==acc)]
                kinds={k:[r for r in support if r['data'].get('kind')==k] for k in ('actual','budget')}
                if kinds['actual'] and kinds['budget']:
                    if acc=='PAYROLL':
                        aq=sum(Decimal(str(r['data']['fte'])) for r in kinds['actual']);bq=sum(Decimal(str(r['data']['fte'])) for r in kinds['budget'])
                        sa=sum(r['amount'] for r in kinds['actual']);sb=sum(r['amount'] for r in kinds['budget'])
                        compatible=aq>0 and bq>0 and sa==ma and sb==mb
                        if compatible:
                            make_driver(fact,'Payroll',remaining,'supported','actual FTE × actual loaded cost − budget FTE × budget loaded cost',{'month':month,'actual_fte':str(aq),'budget_fte':str(bq),'actual_loaded_cost_minor':str(Decimal(sa)/aq),'budget_loaded_cost_minor':str(Decimal(sb)/bq),'actual_minor':ma,'budget_minor':mb},source_ids+[r['id'] for r in support],'payroll')
                            remaining=0
                    elif len(kinds['actual'])==len(kinds['budget'])==1:
                        sr=kinds['actual'][0];tr=kinds['budget'][0];av=sr['data'];bv=tr['data']
                        aq=Decimal(str(av['units']));bq=Decimal(str(bv['units']));ap=Decimal(av['rate_minor']);bp=Decimal(bv['rate_minor'])
                        compatible=av.get('unit') and av.get('unit')==bv.get('unit') and rounded(aq*ap)==ma and rounded(bq*bp)==mb
                        if compatible:
                            quantity=rounded((aq-bq)*bp);rate=remaining-quantity
                            inp={'month':month,'actual_units':str(aq),'budget_units':str(bq),'actual_rate_minor':int(ap),'budget_rate_minor':int(bp),'unit':av['unit'],'interaction_assigned_to':'rate'}
                            make_driver(fact,'Usage',quantity,'supported','(actual units − budget units) × budget rate',inp,source_ids+[sr['id'],tr['id']],'quantity')
                            make_driver(fact,'Rate',rate,'supported','actual units × (actual rate − budget rate); rounding residual assigned here',inp,source_ids+[sr['id'],tr['id']],'rate')
                            remaining=0
                # Explicit reviewed annotations may attribute a disjoint portion of the remaining variance.
                journals={(r['data']['journal_id'],r['data']['line_id']) for r in ar}
                allocations=[r for r in rows if r['role']=='allocations' and (r['data']['journal_id'],r['data']['line_id']) in journals]
                invoice_ids={r['data']['invoice_id'] for r in allocations}
                annotated=set()
                for inv in rows:
                    d=inv['data']
                    if inv['role']!='invoices' or d['invoice_id'] not in invoice_ids or d['invoice_id'] in annotated or str(d.get('evidence_reviewed','')).lower()!='true':continue
                    linked_allocations=[r for r in rows if r['role']=='allocations' and r['data']['invoice_id']==d['invoice_id']]
                    if d.get('month')!=month or any((r['data']['journal_id'],r['data']['line_id']) not in journals for r in linked_allocations):continue
                    annotated.add(d['invoice_id']);contribution=d.get('variance_amount_minor',0)
                    if contribution and contribution*remaining>0 and abs(contribution)<=abs(remaining) and d.get('variance_label'):
                        make_driver(fact,str(d['variance_label']),contribution,'supported','reviewed invoice attribution; bounded by remaining account variance',{'month':month,'reviewed_contribution_minor':contribution,'remaining_before_minor':remaining,'invoice_id':d['invoice_id']},source_ids+[inv['id']]+[r['id'] for r in allocations if r['data']['invoice_id']==d['invoice_id']],'reviewed_annotation')
                        remaining-=contribution
                make_driver(fact,'Unexplained',remaining,'unknown','account variance − disjoint supported contributions',{'month':month,'actual_minor':ma,'budget_minor':mb,'unexplained_minor':remaining},source_ids,'residual')
        assert sum(d['amount_minor'] for d in drivers if d['fact_id']==fact['id'])==var
        put('fact',fact,wid);facts.append(fact)
    a=sum(f['actual_minor'] for f in facts);b=sum(f['budget_minor'] for f in facts);gross=sum(abs(d['amount_minor']) for d in drivers);covered=sum(abs(d['amount_minor']) for d in drivers if d['evidence_state'] in STATES)
    timeline=[]
    for m in periods:
        timeline.append({'month':m,'actual_minor':sum(r['amount'] for r in selected if r['role']=='actuals' and r['data']['month']==m),'budget_minor':sum(r['amount'] for r in selected if r['role']=='budgets' and r['data']['month']==m)})
    alerts=investigate(rows,period,policy,periods)
    for alert in alerts:
        linked=next((f for f in facts if set(f['source_ids'])&set(alert['source_ids'])),None)
        alert.update(snapshot_id=sid,period=period,scope=scope,calculation_version=VERSION,amount_unit='INR paise',formula='Review rule: '+alert['rule'],amount_minor=alert['inputs'].get('current_minor',alert['inputs'].get('amount_minor',alert['inputs'].get('residual_minor',0))),fact_id=linked['id'] if linked else None)
        put('alert',alert,wid)
    selected_ids={r['id'] for r in actual}
    result={'id':aid,'snapshot_id':sid,'period':period,'scope':scope,'amount_unit':'INR paise','calculation_version':VERSION,'policy':policy,'periods':periods,'filters':{'department':department,'account':account},'departments':sorted({r['data']['department'] for r in rows if r['role']=='actuals'}),'accounts':sorted({r['data']['account'] for r in rows if r['role']=='actuals'}),'totals':{'actual_minor':a,'budget_minor':b,'variance_minor':a-b,'percentage':percentage(a-b,b),'unbudgeted':b==0,'unexplained_minor':sum(d['amount_minor'] for d in drivers if d['evidence_state']=='unknown'),'gross_unexplained_minor':gross-covered},'coverage':{'numerator_minor':covered,'denominator_minor':gross,'percentage':percentage(covered,gross) if gross else 100},'facts':sorted(facts,key=lambda f:abs(f['variance_minor']),reverse=True),'drivers':drivers,'timeline':timeline,'alerts':alerts,'transaction_ids':sorted(selected_ids),'completeness':'sealed','support_warnings':manifest.get('warnings',[]),'report_review_status':'not_started_phase_3'}
    return put('analysis',result,wid)

def investigate(rows,period,policy,periods):
    alerts=[];actual=[r for r in rows if r['role']=='actuals'];history=periods[max(0,periods.index(period)-3):periods.index(period)]
    by_key=defaultdict(lambda:defaultdict(int));sources=defaultdict(list)
    for r in actual:
        d=r['data'];key=(d['department'],d['account'],d.get('vendor','Unassigned'))
        by_key[key][d['month']]+=r['amount'];sources[(key,d['month'])].append(r['id'])
    def alert(rule,label,detail,evidence,inputs):
        alerts.append({'id':digest([rule,period,evidence]),'rule':rule,'label':label,'detail':detail,'source_ids':evidence,'inputs':inputs,'evidence_state':'investigation_prompt'})
    for key,values in sorted(by_key.items()):
        current=values[period]
        if history and current and not any(values[m] for m in periods[:periods.index(period)]):alert('first_vendor','First-observed vendor',f'{key[2]} first appears in {period}. This is a review prompt, not a fraud conclusion.',sources[(key,period)],{'vendor':key[2],'prior_months':periods[:periods.index(period)]})
        if len(history)==3:
            past=[values[m] for m in history];mid=int(median(past))
            if mid>0 and Decimal(current)>Decimal(mid)*(1+Decimal(str(policy['jump_percentage']))/100) and current-mid>policy['jump_amount_minor']:
                alert('spend_jump','Spend above recent history',f"{key[2]} · {ACCOUNT_NAMES.get(key[1],key[1])} exceeds the prior three-month median by over {policy['jump_percentage']}% and the amount threshold.",sum([sources[(key,m)] for m in history+[period]],[]),{'months':history,'history_minor':past,'median_minor':mid,'current_minor':current,'percentage_threshold':policy['jump_percentage'],'amount_threshold_minor':policy['jump_amount_minor']})
    references=defaultdict(list)
    for r in rows:
        if r['role']=='invoices':references[r['data']['invoice_id']].append(r)
        if r['role']=='actuals' and r['data']['month']==period and r['amount']<=-policy['credit_threshold_minor']:
            alert('unusual_credit','Unusual credit','A posted credit exceeds the review threshold. Confirm its nature and period.',[r['id']],{'amount_minor':r['amount'],'threshold_minor':policy['credit_threshold_minor']})
    for ref,group in references.items():
        if len(group)>1:alert('invoice_reference','Repeated invoice reference',f'{ref} occurs on multiple invoice source rows.',[r['id'] for r in group],{'reference':ref,'count':len(group)})
    for dep in {r['data']['department'] for r in actual if r['data']['month']==period and r['data']['account']=='PAYROLL'}:
        ledger=[r for r in actual if r['data']['month']==period and r['data']['department']==dep and r['data']['account']=='PAYROLL'];people=[r for r in rows if r['role']=='headcount' and r['data']['month']==period and r['data']['department']==dep and r['data']['kind']=='actual']
        gap=sum(r['amount'] for r in ledger)-sum(r['amount'] for r in people)
        if gap:alert('payroll_gap','Payroll support gap','Headcount support differs from the posted payroll ledger.',[r['id'] for r in ledger+people],{'ledger_minor':sum(r['amount'] for r in ledger),'support_minor':sum(r['amount'] for r in people),'residual_minor':gap})
    return alerts
