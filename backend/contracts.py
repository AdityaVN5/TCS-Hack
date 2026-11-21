"""Phase 1 contracts. Money is normalized to integer paise; expenses positive."""
CONTRACTS = {
 'budgets': {'required':['month','department','account','amount','budget_version'], 'optional':['currency']},
 'actuals': {'required':['journal_id','line_id','date','department','account','amount'], 'optional':['currency','description','vendor','invoice_id']},
 'invoices': {'required':['invoice_id','date','vendor','amount'], 'optional':['currency','description','variance_amount','variance_label','evidence_reviewed']},
 'allocations': {'required':['invoice_id','journal_id','line_id','amount'], 'optional':['currency']},
 'headcount': {'required':['employee_id','month','department','fte','amount','kind'], 'optional':['currency']},
 'units_rates': {'required':['month','department','account','units','rate','kind'], 'optional':['currency','unit']},
 'planned_positions': {'required':['position_id','department','start_date','amount','committed'], 'optional':['currency']},
 'cash': {'required':['month','opening_cash','receipts','payments'], 'optional':['currency','recurring_vendor','existing_payroll','other_operating','taxes','capex','debt_service','scheduled_invoices','planned_hires','breakdown_reviewed','vendor_cancellable']},
 'obligations': {'required':['obligation_id','date','amount','category','committed'], 'optional':['currency','invoice_id']},
}
PARSER_VERSION = 'trabo-parser-1.0'
ENGINE_VERSION = 'trabo-close-1.1'
