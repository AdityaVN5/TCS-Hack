import json
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import duckdb
from store import ROOT, all_items, get, put, workspace, digest, now
from contracts import CONTRACTS, PARSER_VERSION, ENGINE_VERSION

def money(value, scale='rupees'):
    try:
        number = Decimal(str(value).replace(',', '').strip())
        multiplier = {'rupees':100,'paise':1,'thousands':100000,'lakhs':10000000}[scale]
        scaled = number * multiplier
        if not scaled.is_finite() or scaled != scaled.to_integral_value():
            raise ValueError('Amount must resolve to whole paise.')
        if abs(scaled) > 9_000_000_000_000_000:
            raise ValueError('Amount exceeds supported range.')
        return int(scaled)
    except (InvalidOperation, KeyError) as e:
        raise ValueError('Invalid monetary amount.') from e

def date_value(value, fmt='ISO'):
    val = str(value).strip()
    if len(val) == 7 and val[4] == '-':
        return datetime.strptime(val,'%Y-%m').strftime('%Y-%m')
    if '/' in val:
        if fmt == 'ISO':
            raise ValueError('Ambiguous date: choose DMY or MDY in the mapping.')
        return datetime.strptime(val, '%d/%m/%Y' if fmt == 'DMY' else '%m/%d/%Y').strftime('%Y-%m-%d')
    return datetime.fromisoformat(val).date().isoformat()

def input_fingerprint(wid):
    uploads = all_items('upload',wid)
    return digest({'inputs': sorted([(u['hash'],[(s['name'],s.get('mapping')) for s in u['sheets']]) for u in uploads]), 'policy':workspace(wid), 'corrections':sorted(all_items('correction',wid),key=lambda x:x['id']), 'engine':ENGINE_VERSION})

def validate(wid):
    policy = workspace(wid)
    issues, records = [], []
    seen = set()
    def issue(code, title, detail, record=None, severity='blocker', actions=None, extra=None):
        iid = digest([code,record and record['id'],extra])[:24]
        correction = get('correction',iid,wid)
        issues.append({'id':iid,'code':code,'title':title,'detail':detail,'severity':severity,'record_id':record and record['id'],'file':record and record['source']['filename'],'row':record and record['source']['row'],'amount':record and record.get('amount',0) or 0,'actions':actions or [],'status':'resolved' if correction and correction.get('action')=='acknowledge' else 'open', 'extra':extra})
        return iid
    # Stable traversal makes duplicate selection independent of mapping save order.
    for upload in sorted(all_items('upload',wid),key=lambda u:u['hash']):
        for sheet in upload['sheets']:
            mapping = sheet.get('mapping')
            if not mapping:
                issue('mapping_required','Confirm column mapping',f"{upload['name']} · {sheet['name']} needs a confirmed mapping.",extra=[upload['id'],sheet['name']])
                continue
            role = mapping['role']
            for rownum, raw in enumerate(sheet['rows'],2):
                rid = digest([wid,policy['entity'],upload['hash'],sheet['name'],rownum,role])[:24]
                base_correction = get('correction',rid,wid) or {}
                correction = base_correction
                data = {k:raw.get(v) for k,v in mapping['columns'].items() if v}
                data.update(correction.get('values',{}))
                expanded = [(None,data)]
                if mapping.get('wide_months'):
                    expanded = [(month,dict(data,month=month,amount=raw.get(col))) for month,col in mapping['wide_months'].items()]
                for wide_month, values in expanded:
                    canonical_id = digest([rid,wide_month])[:24] if wide_month else rid
                    correction = get('correction',canonical_id,wid) or base_correction
                    values.update(correction.get('values',{}))
                    record = {'id':canonical_id,'role':role,'data':values,'amount':0,'accepted':True,'source':{'file_hash':upload['hash'],'filename':upload['name'],'sheet':sheet['name'],'row':rownum,'columns':mapping['columns'],'wide_months':mapping.get('wide_months',{}),'parser_version':PARSER_VERSION,'mapping_version':digest(mapping),'workspace':wid,'entity':policy['entity']}}
                    records.append(record)
                    excluded = correction.get('action')=='exclude'
                    try:
                        if 'amount' in values:
                            record['amount'] = money(values['amount'],mapping['scale']) * (-1 if mapping['sign']=='negative' and role in ('actuals','budgets') else 1)
                    except ValueError as e:
                        record['accepted']=False
                        if not excluded:
                            issue('invalid_amount','Invalid monetary amount',str(e),record,actions=['correct','exclude'])
                    if excluded:
                        record['accepted']=False
                        record['excluded']=True
                        continue
                    try:
                        for key in CONTRACTS[role]['required']:
                            if values.get(key) is None or str(values[key]).strip()=='':
                                raise ValueError(f'Required value missing: {key}.')
                        for m in sheet.get('missing_formula_values',[]):
                            if m['row']==rownum and m['column'] in mapping['columns'].values() and not correction.get('values'):
                                raise ValueError(f"Formula in {m['column']} has no cached value. Enter the reviewed value.")
                        currency = values.get('currency') or 'INR'
                        if currency != 'INR':
                            raise ValueError('Mixed or unsupported currency. Reporting requires INR.')
                        for field in ('date','month','start_date'):
                            if field in values:
                                values[field] = date_value(values[field],mapping.get('date_format','ISO'))
                                if field=='month': values[field]=values[field][:7]
                        if 'date' in values:
                            values['month']=values['date'][:7]
                        if 'department' in values:
                            values['department']=policy['aliases'].get(values['department'],values['department'])
                            if values['department'] not in policy['departments']:
                                raise ValueError('Unknown department. Correct the ID or add a workspace alias.')
                        if 'account' in values and values['account'] not in policy['accounts']:
                            raise ValueError('Unknown account ID.')
                        if role=='budgets' and values['budget_version']!=policy['budget_version']:
                            raise ValueError('Budget version differs from the approved workspace version.')
                        if role in ('actuals','budgets','headcount','units_rates') and values['month'] not in reporting_months(policy['period']):
                            raise ValueError('Record falls outside the six historical reporting months.')
                        if role=='headcount':
                            if not str(values['employee_id']).startswith('EMP-'):
                                raise ValueError('Use pseudonymous employee IDs beginning EMP-.')
                            fte=Decimal(str(values['fte']))
                            if not fte.is_finite() or fte<0: raise ValueError('FTE must be finite and nonnegative.')
                        if role in ('headcount','units_rates') and values['kind'] not in ('budget','actual'):
                            raise ValueError('Kind must be budget or actual.')
                        if role=='units_rates':
                            values['rate_minor']=money(values['rate'],mapping['scale'])
                            units=Decimal(str(values['units']))
                            if not units.is_finite() or units<0: raise ValueError('Units must be finite and nonnegative.')
                        if role=='cash':
                            for key in ('opening_cash','receipts','payments','recurring_vendor','existing_payroll','other_operating','taxes','capex','debt_service','scheduled_invoices','planned_hires'):
                                if key not in values or values[key] in (None,''): continue
                                values[key+'_minor']=money(values[key],mapping['scale'])
                        if role=='invoices' and values.get('variance_amount') not in (None,''):
                            values['variance_amount_minor']=money(values['variance_amount'],mapping['scale'])
                        if role in ('planned_positions','obligations') and str(values['committed']).lower() not in ('true','false'):
                            raise ValueError('Committed must be true or false.')
                        if role=='actuals':
                            key=('journal',values['journal_id'],values['line_id'])
                        elif role=='budgets':
                            key=('budget',values['budget_version'],values['month'],values['department'],values['account'])
                        else:
                            key=None
                        if key and key in seen:
                            record['accepted']=False
                            issue('duplicate_key','Duplicate journal key' if role=='actuals' else 'Duplicate budget grain','This key already exists. Keep one source row and explicitly exclude the duplicate.',record,actions=['exclude','correct'])
                        if key: seen.add(key)
                    except (ValueError,InvalidOperation) as e:
                        record['accepted']=False
                        issue('invalid_row','Source row needs attention',str(e),record,actions=['correct','exclude'])
                    if role=='invoices' and any(t in str(values.get('description','')).lower() for t in ('<script','ignore previous','system prompt')):
                        issue('untrusted_text','Untrusted description retained','Description is inert source text; review it before using it in later reporting.',record,'warning',['acknowledge'])
    accepted = [r for r in records if r['accepted']]
    budgets = {(r['data']['month'],r['data']['department'],r['data']['account']) for r in accepted if r['role']=='budgets'}
    ledger = [r for r in accepted if r['role']=='actuals']
    missing=set()
    for r in ledger:
        key=(r['data']['month'],r['data']['department'],r['data']['account'])
        if key not in budgets and key not in missing:
            missing.add(key)
            iid=digest(['missing_budget',r['id'],list(key)])[:24]
            correction=get('correction',iid,wid)
            if correction and correction.get('action')=='approve_zero':
                z={'id':iid,'role':'budgets','data':dict(zip(('month','department','account'),key),budget_version=policy['budget_version'],amount='0',approved_zero=True),'amount':0,'accepted':True,'source':dict(r['source'],correction_id=iid,origin='reviewer-approved zero budget')}
                records.append(z)
            else:
                issue('missing_budget','Missing comparison budget','No approved budget exists for this month, department and account. Upload a budget or explicitly approve a zero.',r,actions=['approve_zero'],extra=list(key))
    invoices = {r['data']['invoice_id']:r for r in accepted if r['role']=='invoices'}
    ledger_keys={(r['data']['journal_id'],r['data']['line_id']):r for r in ledger}
    support=defaultdict(int)
    invoice_support=defaultdict(int)
    allocation_rows=defaultdict(list)
    invoice_allocation_rows=defaultdict(list)
    for r in accepted:
        if r['role']=='allocations':
            key=(r['data']['journal_id'],r['data']['line_id'])
            if key not in ledger_keys or r['data']['invoice_id'] not in invoices:
                r['accepted']=False
                issue('orphan_allocation','Allocation has no matching source','Both its journal line and invoice must exist.',r,actions=['exclude','correct'])
            else:
                support[key]+=r['amount']
                invoice_support[r['data']['invoice_id']]+=r['amount']
                allocation_rows[key].append(r)
                invoice_allocation_rows[r['data']['invoice_id']].append(r)
                if r['amount']*ledger_keys[key]['amount']<0 or r['amount']*invoices[r['data']['invoice_id']]['amount']<0:
                    issue('allocation_sign','Allocation sign does not match its sources','An allocation must have the same sign as its journal line and invoice.',r,actions=['correct','exclude'])
    for key, amount in support.items():
        if abs(amount)>abs(ledger_keys[key]['amount']) or amount*ledger_keys[key]['amount']<0:
            for allocation in allocation_rows[key]:
                issue('allocation_overflow','Support exceeds ledger amount','Invoice allocations cannot multiply or exceed ledger spend. Correct or exclude an allocation row.',allocation,actions=['correct','exclude'])
    for inv_id,r in invoices.items():
        if not invoice_support[inv_id]:
            issue('unmatched_invoice','Invoice is not matched to the ledger','Supporting AP detail is excluded from ledger spend. Add an allocation or retain this visible warning.',r,'warning',['acknowledge'])
        elif abs(invoice_support[inv_id])>abs(r['amount']) or invoice_support[inv_id]*r['amount']<0:
            for allocation in invoice_allocation_rows[inv_id]:
                issue('invoice_overflow','Allocations exceed invoice amount','Correct or exclude the allocation before confirming.',allocation,actions=['correct','exclude'])
    refs=defaultdict(list)
    for r in accepted:
        if r['role']=='invoices': refs[(r['data']['vendor'],r['data']['date'],r['amount'])].append(r)
    for group in refs.values():
        if len(group)>1:
            issue('possible_invoice_duplicate','Possible duplicate invoice','Same vendor, date, and amount. Review supporting records; AP is never added to ledger spend.',group[-1],'warning',['acknowledge'])
    invoice_ids=defaultdict(list)
    for r in accepted:
        if r['role']=='invoices': invoice_ids[r['data']['invoice_id']].append(r)
    for group in invoice_ids.values():
        if len(group)>1:
            issue('duplicate_invoice_reference','Repeated invoice reference','Multiple source rows use this invoice ID. Review them before relying on their supporting detail.',group[-1],'warning',['acknowledge'])
    if not ledger:
        issue('missing_actuals','Actuals required','Upload and map at least one valid ledger file.')
    else:
        present={r['data']['month'] for r in ledger}
        absent=[m for m in reporting_months(policy['period']) if m not in present]
        if absent:
            issue('missing_periods','Historical periods are incomplete','Upload ledger entries for all six historical months. Missing: '+', '.join(absent),extra=absent)
    totals={}
    for role in CONTRACTS:
        rows=[r for r in records if r['role']==role]
        totals[role]={'raw':sum(r['amount'] for r in rows),'accepted':sum(r['amount'] for r in rows if r['accepted']),'quarantined':sum(r['amount'] for r in rows if not r['accepted']),'rows':len(rows),'quarantined_rows':sum(not r['accepted'] for r in rows)}
    matched=sum(support.values())
    ledger_total=sum(r['amount'] for r in ledger)
    current_ids={i['id'] for i in issues}
    for old in all_items('issue_history',wid):
        if old['id'] not in current_ids:
            old['status']='resolved'
            put('issue_history',old,wid)
    for item in issues:
        put('issue_history',item,wid)
    result={'id':'current','created_at':now(),'fingerprint':input_fingerprint(wid),'issues':issues,'records':records,'totals':totals,'blockers':sum(i['severity']=='blocker' and i['status']=='open' for i in issues),'warnings':sum(i['severity']=='warning' for i in issues),'reconciliation':{'raw_equals_accepted_plus_quarantined':all(t['raw']==t['accepted']+t['quarantined'] for t in totals.values()),'ledger':ledger_total,'matched_support':matched,'labeled_residual':ledger_total-matched,'enriched_ledger':ledger_total}}
    put('validation',result,wid)
    return result

def reporting_months(period):
    year,month=map(int,period.split('-'))
    index=year*12+month-1
    return [f'{n//12:04d}-{n%12+1:02d}' for n in range(index-5,index+1)]

def publish(wid,actor=None):
    result=validate(wid)
    if result['blockers']:
        raise ValueError('Resolve all blocking issues before confirming reconciled data.')
    sid=result['fingerprint']
    existing=get('snapshot',sid,wid)
    if existing: return existing
    directory=ROOT/'snapshots'/wid/sid
    directory.mkdir(parents=True,exist_ok=True)
    rows=[r for r in result['records'] if r['accepted']]
    connection=duckdb.connect()
    temp=directory/'canonical.tmp.parquet'
    try:
        connection.execute('CREATE TABLE canonical (record_id VARCHAR, role VARCHAR, amount_minor BIGINT, payload JSON)')
        connection.executemany('INSERT INTO canonical VALUES (?,?,?,?)',[(r['id'],r['role'],r['amount'],json.dumps(r)) for r in rows])
        connection.execute('COPY canonical TO ? (FORMAT PARQUET)',[str(temp)])
        os.replace(temp,directory/'canonical.parquet')
    finally:
        connection.close()
    manifest={'id':sid,'created_at':now(),'workspace':wid,'engine_version':ENGINE_VERSION,'amount_unit':'INR paise','policy':workspace(wid),'record_count':len(rows),'totals':result['totals'],'reconciliation':result['reconciliation'],'warnings':[i for i in result['issues'] if i['severity']=='warning'],'issue_history':all_items('issue_history',wid),'inputs':[{'hash':u['hash'],'name':u['name'],'mappings':[s.get('mapping') for s in u['sheets']]} for u in all_items('upload',wid)],'corrections':all_items('correction',wid),'correction_revisions':all_items('correction_revision',wid)}
    manifest['mapping_revisions']=all_items('mapping_revision',wid)
    manifest['confirmed_by']=actor
    (directory/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return put('snapshot',manifest,wid)
