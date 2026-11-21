import re
from decimal import Decimal
from pydantic import ValidationError
from report_models import ReportDocument, SECTION_TITLES, SCHEMA_VERSION

VERSION='trabo-report-render-1.0'
PROMPT_VERSION='trabo-grounded-report-1.0'
GENERATION_VERSION='trabo-report-generation-1.0'

def direction(amount):return 'adverse' if amount>0 else 'favorable' if amount<0 else 'neutral'

def money(amount):
    if amount is None:return 'unavailable'
    number=Decimal(amount)/100
    return f'INR {number:,.2f}'

def catalog(packet):
    period=packet['period'];totals=packet['totals'];refs={}
    def ref(key,template,label,amount,state,d='not_applicable',pct=None,**extra):
        refs[key]={'reference_id':key,'template':template,'label':label,'amount_minor':amount,'percentage':str(pct) if pct is not None else None,'evidence_state':state,'direction':d,'period':period,'amount_unit':'INR paise',**extra}
    ref('summary','summary','Total expense movement',totals['variance_minor'],'verified_arithmetic',direction(totals['variance_minor']),totals['percentage'])
    ref('coverage','coverage','Explanation coverage',packet['coverage']['numerator_minor'],'verified_arithmetic',pct=packet['coverage']['percentage'])
    ref('unknown_total','unknown','Gross unexplained movement',totals['gross_unexplained_minor'],'unknown')
    ref('scope','scope','Reporting scope and source version',None,'verified_arithmetic')
    ref('assumptions','assumptions','Cash assumptions and exclusions',None,'modeled')
    cash=packet['scenario']['result'] if packet['scenario'] else packet['baseline_cash']
    ref('cash_baseline','cash','Baseline closing cash',cash['summary']['baseline_closing_minor'],'modeled')
    if packet['scenario']:
        ref(packet['scenario']['id'],'cash','Selected scenario closing cash',cash['summary']['scenario_closing_minor'],'modeled')
    for f in packet['facts']:
        ref(f['id'],'movement',f['label'],f['variance_minor'],'verified_arithmetic',direction(f['variance_minor']),f['percentage'])
    for d in packet['drivers']:
        ref(d['id'],'unknown' if d['evidence_state']=='unknown' else 'driver',d['label'],d['amount_minor'],d['evidence_state'],direction(d['amount_minor']))
    for n in packet['owner_notes']:
        ref(n['id'],'owner_note',n['owner'],None,n['evidence_state'])
    return refs

def claim_for(binding):
    return {k:binding[k] for k in ('template','reference_id','period','amount_unit','direction','evidence_state')}|{'wording':'standard','amount_minor':None,'percentage':None}

def template_document(packet):
    refs=catalog(packet)
    material=set(packet['material_fact_ids'])
    sections=[{'key':key,'commentary':'','claims':[]} for key in SECTION_TITLES]
    by_key={s['key']:s for s in sections}
    def add(key,rid):by_key[key]['claims'].append(claim_for(refs[rid]))
    add('executive','summary');add('executive','coverage')
    for f in packet['facts']:
        if f['id'] in material:add('comparison',f['id'])
    for d in packet['drivers']:
        if d['fact_id'] in material and d['evidence_state']!='unknown':add('movements',d['id'])
    add('unknowns','unknown_total')
    for d in packet['drivers']:
        if d['fact_id'] in material and d['evidence_state']=='unknown':add('unknowns',d['id'])
    add('cash','cash_baseline')
    if packet['scenario']:add('cash',packet['scenario']['id'])
    add('cash','assumptions');add('evidence','scope')
    ref=next((d['id'] for d in packet['drivers'] if d['evidence_state']=='unknown'),'summary')
    return {'schema_version':SCHEMA_VERSION,'snapshot_id':packet['snapshot_id'],'period':packet['period'],'amount_unit':'INR paise','sections':sections,'actions':[{'reference_id':ref,'instruction':'Review the supporting records and document any remaining explanation.','owner':''}],'questions':[{'reference_id':'unknown_total','text':'What additional source support is needed to explain the remaining movement?'}] if packet['totals']['gross_unexplained_minor'] else []}

# Prose is for review questions/context. Financial and temporal claims must use bindings.
NUMERIC=re.compile(r'\d|[₹$€£%]|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|half|double|triple|hundred|thousand|million|billion|lakh|crore|percent|january|february|march|april|may|june|july|august|september|october|november|december)\b',re.I)
CAUSAL=re.compile(r'\b(?:because|cause|causes|caused|causing|due to|driven by|drives|led to|attributable to|resulted in|fraud|proves?|guaranteed|definitely|certainly|will save|will increase|will decrease)\b',re.I)
RELATIONSHIP=re.compile(r'\b(?:favorable|adverse|unbudgeted|overrun|underspend|increased?|decreased?|rose|fell|reduced|improved|deteriorated|higher|lower|doubled|tripled)\b',re.I)

def validate_document(raw,packet):
    errors=[];warnings=[];linked=0;count=0
    try:doc=ReportDocument.model_validate(raw).model_dump()
    except ValidationError as e:
        return {'valid':False,'errors':[{'code':'schema','path':'.'.join(map(str,x['loc'])),'message':x['msg']} for x in e.errors()],'warnings':[],'traceability_percentage':0,'linked_claims':0,'total_claims':0,'unassigned_actions':0,'requires_human_review':True}
    def error(code,path,message):errors.append({'code':code,'path':path,'message':message})
    def prose(text,path):
        if NUMERIC.search(text):error('unbound_claim',path,'Use a linked claim for figures, percentages, or reporting months; prose cannot introduce them.')
        if CAUSAL.search(text):error('unsupported_cause',path,'A causal or certain assertion needs controlled evidence-backed wording.')
        if RELATIONSHIP.search(text):error('unbound_relationship',path,'Use a linked claim for financial direction, comparisons, or movements.')
    if doc['snapshot_id']!=packet['snapshot_id']:error('snapshot','snapshot_id','Document snapshot differs from its sealed evidence packet.')
    if doc['period']!=packet['period']:error('period','period','Document reporting month differs from its evidence packet.')
    refs=catalog(packet);seen=set();section_keys=[s['key'] for s in doc['sections']]
    if section_keys!=list(SECTION_TITLES):error('sections','sections','Keep each required section exactly once and in the report outline order.')
    for si,section in enumerate(doc['sections']):
        prose(section['commentary'],f'sections.{si}.commentary')
        for ci,claim in enumerate(section['claims']):
            path=f'sections.{si}.claims.{ci}';count+=1;r=refs.get(claim['reference_id'])
            if not r:error('reference',path,'Claim reference does not exist in this snapshot, scope, and packet.');continue
            ok=True
            for field in ('template','period','amount_unit','direction','evidence_state'):
                if claim[field]!=r[field]:error(field,path+'.'+field,'Claim '+field+' does not match its bound evidence.');ok=False
            if claim['amount_minor'] is not None and claim['amount_minor']!=r['amount_minor']:error('amount',path+'.amount_minor','Edited figure does not equal the stored financial fact.');ok=False
            if claim['percentage'] is not None:
                try:matches=r['percentage'] is not None and Decimal(claim['percentage'])==Decimal(r['percentage'])
                except Exception:matches=False
                if not matches:error('percentage',path+'.percentage','Edited percentage does not equal the stored financial fact.');ok=False
            if claim['reference_id'] in seen:error('duplicate_claim',path,'A contribution may be stated once; duplicate claims are not additive evidence.');ok=False
            seen.add(claim['reference_id']);linked+=int(ok)
    required={'summary','coverage','unknown_total','scope','assumptions','cash_baseline',*packet['material_fact_ids']}
    required.update(d['id'] for d in packet['drivers'] if d['fact_id'] in packet['material_fact_ids'])
    if packet['scenario']:required.add(packet['scenario']['id'])
    for rid in sorted(required-seen):error('omission','sections','Required evidence or disclosure is missing: '+rid)
    if not any(c['reference_id']=='unknown_total' for s in doc['sections'] if s['key']=='unknowns' for c in s['claims']):error('unknown_disclosure','sections.unknowns','Disclose remaining unknowns in the unexplained amounts section.')
    if packet['totals']['gross_unexplained_minor'] and not any(q['reference_id']=='unknown_total' for q in doc['questions']):error('open_question','questions','Keep an open review question for the unexplained balance.')
    for i,item in enumerate(doc['actions']):
        if item['reference_id'] not in refs:error('reference',f'actions.{i}','Action reference is not in the packet.')
        prose(item['instruction'],f'actions.{i}.instruction')
        if not re.match(r'^(?:Review|Confirm|Investigate|Reconcile|Request|Document|Assess|Validate|Check)\b',item['instruction'],re.I):error('action',f'actions.{i}.instruction','Phrase actions as proposed review or investigation work.')
    for i,q in enumerate(doc['questions']):
        if q['reference_id'] not in refs:error('reference',f'questions.{i}','Question reference is not in the packet.')
        prose(q['text'],f'questions.{i}.text')
        if not q['text'].rstrip().endswith('?'):error('question',f'questions.{i}.text','Open questions must be phrased as questions.')
    unassigned=sum(not a['owner'].strip() for a in doc['actions'])
    if unassigned:warnings.append({'code':'owners','message':f'Assign an owner to each proposed action before approval ({unassigned} remaining).'})
    return {'valid':not errors,'errors':errors,'warnings':warnings,'traceability_percentage':round(linked/count*100,2) if count else 0,'linked_claims':linked,'total_claims':count,'unassigned_actions':unassigned,'requires_human_review':True}

def render_claim(claim,packet):
    refs=catalog(packet);r=refs.get(claim['reference_id'])
    if not r:return 'Unresolved evidence reference. This claim cannot be approved.'
    rid=r['reference_id'];kind=r['template'];concise=claim['wording']=='concise'
    if kind=='summary':
        t=packet['totals'];change='unbudgeted' if t['percentage'] is None else f"{t['percentage']:g}%"
        return f"Actual expenses of {money(t['actual_minor'])} compare with {money(t['budget_minor'])} {'prior-month actual' if packet['scope']=='mom' else 'approved budget'}, a {money(abs(t['variance_minor']))} {direction(t['variance_minor'])} movement ({change})."
    if kind=='movement':
        f=next(f for f in packet['facts'] if f['id']==rid)
        return f"{f['label']} ({f['department']}): {money(f['actual_minor'])} actual, {money(f['budget_minor'])} comparison; {money(abs(f['variance_minor']))} {f['direction']} movement."
    if kind=='driver':
        d=next(d for d in packet['drivers'] if d['id']==rid)
        return f"{d['label']}: {money(d['amount_minor'])} supported contribution." if concise else f"{d['label']} contributes {money(d['amount_minor'])} to the movement, supported by the linked calculation and source records."
    if kind=='unknown':
        if rid=='unknown_total':return f"Gross unexplained movement is {money(packet['totals']['gross_unexplained_minor'])}; the signed unexplained balance is {money(packet['totals']['unexplained_minor'])}. It remains open for investigation."
        return f"{r['label']}: {money(r['amount_minor'])} remains unexplained. No business cause has been established."
    if kind=='coverage':return f"Supported explanation coverage is {packet['coverage']['percentage']:g}%: {money(packet['coverage']['numerator_minor'])} of {money(packet['coverage']['denominator_minor'])} in disjoint gross contributions."
    if kind=='cash':
        if r['amount_minor'] is None:return 'Complete cash assumptions are unavailable. Cash balances and runway are not asserted.'
        cash=packet['scenario']['result'] if packet['scenario'] else packet['baseline_cash'];label='Selected scenario' if rid!='cash_baseline' else 'Baseline'
        return f"{label} closing cash is {money(r['amount_minor'])} at the end of the modeled horizon ({cash['periods'][0]} to {cash['periods'][-1]}). This is a modeled outcome under the saved assumptions."
    if kind=='assumptions':return 'Cash projections depend on reviewed receipts and exclusive payment buckets. Vendor changes exclude commitments; hiring freezes apply only to eligible planned roles from their start dates. Modeled savings are not actual results.'
    if kind=='owner_note':return f"The owner {r['label']} recorded context for review. Owner notes remain separate from verified arithmetic and supported attribution."
    return f"Reporting period {packet['period']}; scope {packet['scope']}; approved budget {packet['budget_version']}; source snapshot {packet['snapshot_id'][:12]}. All amounts are reported in INR; stored monetary facts use integer paise."

def render_document(doc,packet):
    sections=[]
    for s in doc['sections']:
        sections.append({'key':s['key'],'title':SECTION_TITLES[s['key']],'commentary':s['commentary'],'claims':[{'reference_id':c['reference_id'],'text':render_claim(c,packet),'evidence_state':c['evidence_state']} for c in s['claims']]})
    return {'title':f"{packet['company']} · Management report",'period':packet['period'],'sections':sections,'actions':doc['actions'],'questions':doc['questions']}
