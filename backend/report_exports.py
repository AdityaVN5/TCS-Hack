"""Exports use only the frozen approved payload. Provider code is never called here."""
import csv
import io
from decimal import Decimal
from html import escape
from pathlib import Path
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle, KeepTogether
from report_content import money

def csv_cell(value):
    if isinstance(value,(int,float,Decimal)):return str(value)
    text=str(value if value is not None else '')
    if text.lstrip(' \t\r\n').startswith(('=','+','-','@')) or text.startswith(('\t','\r','\n')):return "'"+text
    return text

def financial_csv(payload,payload_hash):
    out=io.StringIO(newline='');writer=csv.writer(out)
    def row(*values):writer.writerow([csv_cell(v) for v in values])
    packet=payload['packet']
    row('TRABO approved financial tables','Report',payload['report_id'],'Revision',payload['revision'])
    row('Snapshot',packet['snapshot_id'],'Period',packet['period'],'Amount unit','INR rupees')
    row('Approved by',payload['approval']['reviewer'],'Approved at',payload['approval']['approved_at'])
    row('Payload SHA-256',payload_hash)
    row('Account','Department','Comparison INR','Actual INR','Variance INR','Variance percent','Evidence fact ID')
    for f in packet['facts']:row(f['label'],f['department'],Decimal(f['budget_minor'])/100,Decimal(f['actual_minor'])/100,Decimal(f['variance_minor'])/100,'Unbudgeted' if f['percentage'] is None else Decimal(str(f['percentage'])),f['id'])
    t=packet['totals'];row('Total','',Decimal(t['budget_minor'])/100,Decimal(t['actual_minor'])/100,Decimal(t['variance_minor'])/100,'Unbudgeted' if t['percentage'] is None else Decimal(str(t['percentage'])))
    row();row('Bridge contribution','Evidence state','Contribution INR','Driver ID')
    for d in packet['drivers']:row(d['label'],d['evidence_state'],Decimal(d['amount_minor'])/100,d['id'])
    if packet['scenario']:
        result=packet['scenario']['result'];row();row('Selected scenario',packet['scenario']['name'],'Assumptions hash',result['assumptions_hash'])
        row('Month','Baseline closing INR','Scenario closing INR','Vendor saving INR','Hiring saving INR','Action cost INR')
        for i,p in enumerate(result['payment_schedule']):
            row(p['month'],Decimal(result['baseline'][i]['closing_minor'])/100 if result['cash_available'] else 'Unavailable',Decimal(result['scenario'][i]['closing_minor'])/100 if result['cash_available'] else 'Unavailable',Decimal(p['vendor_saving_minor'])/100,Decimal(p['hiring_saving_minor'])/100,Decimal(p['one_time_cost_minor'])/100)
    row();row('Proposed action','Owner','Evidence reference')
    for action in payload['document']['actions']:row(action['instruction'],action['owner'],action['reference_id'])
    return out.getvalue().encode('utf-8-sig')

def report_pdf(payload,payload_hash):
    # The bundled Vera fonts make rendering reproducible without external font downloads.
    fonts=Path(rl_config.__file__).parent/'fonts'
    if 'Trabo' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('Trabo',str(fonts/'Vera.ttf')))
        pdfmetrics.registerFont(TTFont('Trabo-Bold',str(fonts/'VeraBd.ttf')))
        pdfmetrics.registerFontFamily('Trabo',normal='Trabo',bold='Trabo-Bold',italic='Trabo',boldItalic='Trabo-Bold')
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='BodyReport',fontName='Trabo',fontSize=9,leading=15,textColor=colors.HexColor('#51515b'),spaceAfter=8))
    styles.add(ParagraphStyle(name='SmallReport',parent=styles['BodyReport'],fontSize=7,leading=11,textColor=colors.HexColor('#858590')))
    styles.add(ParagraphStyle(name='TitleReport',fontName='Trabo-Bold',fontSize=25,leading=32,textColor=colors.HexColor('#1d1d1f'),spaceAfter=16))
    styles.add(ParagraphStyle(name='HeadingReport',fontName='Trabo-Bold',fontSize=13,leading=19,spaceBefore=18,spaceAfter=10,textColor=colors.HexColor('#34343d')))
    styles.add(ParagraphStyle(name='CellReport',fontName='Trabo',fontSize=7,leading=11,textColor=colors.HexColor('#555560')))
    def p(text,style='BodyReport'):return Paragraph(escape(str(text)).replace('\n','<br/>'),styles[style])
    def table(headers,rows,widths):
        data=[[p(h,'CellReport') for h in headers]]+[[p(cell,'CellReport') for cell in row] for row in rows]
        value=LongTable(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
        value.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#f4f5f7')),('LINEBELOW',(0,0),(-1,0),.5,colors.HexColor('#ddddE6')),('LINEBELOW',(0,1),(-1,-1),.3,colors.HexColor('#eeeeF3')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
        return value
    out=io.BytesIO();packet=payload['packet'];approval=payload['approval'];content=[];width=174*mm
    content += [p('TRABO / APPROVED REPORT','SmallReport'),p(payload['title'],'TitleReport'),p(f"{packet['company']} | {packet['period']} | Revision {payload['revision']} | {packet['scope']}"),p(f"Approved by {approval['reviewer']} at {approval['approved_at']}.",'SmallReport')]
    if payload.get('synthetic'):content.append(p('SYNTHETIC DEMONSTRATION DATA. This artifact is not a real-company financial approval.','SmallReport'))
    content.append(p(f"Source snapshot: {packet['snapshot_id']}",'SmallReport'))
    content.append(p(f"Frozen payload SHA-256: {payload_hash}",'SmallReport'))
    content.append(p(f"Generation: {payload['generation']['mode']}. Amounts in INR; monetary source facts are stored as integer paise.",'SmallReport'))
    for section in payload['rendered']['sections']:
        content.append(p(section['title'],'HeadingReport'))
        if section['commentary']:content.append(p(section['commentary']))
        for claim in section['claims']:
            content.append(p(claim['text']))
            content.append(p(f"Evidence: {claim['reference_id']} | {claim['evidence_state']}",'SmallReport'))
        if section['key']=='comparison':
            rows=[[f['label'],f['department'],money(f['budget_minor']),money(f['actual_minor']),money(f['variance_minor'])] for f in packet['facts']]
            t=packet['totals'];rows.append(['Total','',money(t['budget_minor']),money(t['actual_minor']),money(t['variance_minor'])])
            content.append(table(['Account','Department','Comparison','Actual','Movement'],rows,[43*mm,29*mm,34*mm,34*mm,34*mm]))
        if section['key']=='unknowns':
            for q in payload['document']['questions']:content.append(p('Open question: '+q['text']))
            for warning in packet['support_warnings']:content.append(p('Remaining source warning: '+warning['title']+'. '+warning['detail']))
        if section['key']=='cash':
            result=packet['scenario']['result'] if packet['scenario'] else packet['baseline_cash']
            if packet['scenario']:content.append(p('Selected saved scenario: '+packet['scenario']['name']))
            content.append(p('Assumptions SHA-256: '+result['assumptions_hash'],'SmallReport'))
            if result['cash_available']:
                rows=[[r['month'],money(result['baseline'][i]['closing_minor']),money(result['scenario'][i]['closing_minor'])] for i,r in enumerate(result['payment_schedule'])]
                content.append(table(['Month','Baseline closing cash','Selected modeled closing cash'],rows,[34*mm,70*mm,70*mm]))
            if packet['scenario']:
                params=result['parameters']
                content.append(p(f"Eligible vendor reduction: {Decimal(params['vendor_cut_bps'])/100}% from {params['vendor_effective_month']}. Frozen positions: {', '.join(params['freeze_position_ids']) or 'none'}. One-time cost: {money(params['one_time_cost_minor'])} in {params['one_time_month']}."))
                if result['input_assumptions'].get('reason'):content.append(p('Reviewed assumption context: '+result['input_assumptions']['reason']))
            for item in result['excluded_commitments']:content.append(p(str(item['id'])+': '+item['reason'],'SmallReport'))
        if section['key']=='actions':
            content.append(table(['Proposed review action','Owner','Evidence'],[[a['instruction'],a['owner'],a['reference_id']] for a in payload['document']['actions']],[86*mm,32*mm,56*mm]))
        if section['key']=='evidence':
            rows=[[r['id'],r['source']['filename'],f"{r['source']['sheet']} / row {r['source']['row']}",r['source']['file_hash'][:16]] for r in packet['evidence']]
            content.append(table(['Canonical record','Original file','Location','Source SHA-256 prefix'],rows,[48*mm,45*mm,38*mm,43*mm]))
            for d in packet['drivers']:content.append(p(f"{d['id']} | {d['formula']} | Inputs: {d['inputs']}",'SmallReport'))
    content += [Spacer(1,9*mm),p('Review decision: '+approval['comment'],'SmallReport'),p('Exported from the exact frozen approved payload. Export does not regenerate commentary. Historical approvals retain their original source version.','SmallReport')]
    def footer(canvas,document):
        canvas.setFont('Trabo',7);canvas.setFillColor(colors.HexColor('#9999a4'));canvas.drawString(18*mm,13*mm,f"TRABO | {packet['period']} | Approved revision {payload['revision']}");canvas.drawRightString(192*mm,13*mm,str(document.page))
    doc=SimpleDocTemplate(out,pagesize=A4,rightMargin=18*mm,leftMargin=18*mm,topMargin=20*mm,bottomMargin=22*mm,title=payload['title'],author='TRABO',pageCompression=1)
    def invariant_canvas(*args,**kwargs):
        kwargs['invariant']=1
        return Canvas(*args,**kwargs)
    doc.build(content,onFirstPage=footer,onLaterPages=footer,canvasmaker=invariant_canvas)
    return out.getvalue()
