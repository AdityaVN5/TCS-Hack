import { chromium } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const base=process.env.TRABO_BASE_URL||'http://127.0.0.1:8001';
const output=path.resolve('../artifacts/release');fs.mkdirSync(output,{recursive:true});
const browser=await chromium.launch({channel:'chrome',headless:true,slowMo:80});
const context=await browser.newContext({viewport:{width:1512,height:1050},acceptDownloads:true,recordVideo:{dir:path.join(output,'video'),size:{width:1440,height:1000}}});
await context.tracing.start({screenshots:true,snapshots:true,sources:false});
const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));const checks=[];
const get=async p=>{const r=await page.request.get(base+p);if(!r.ok())throw new Error(await r.text());return r.json()};
const runJob=async(predicate,action)=>{const [response]=await Promise.all([page.waitForResponse(r=>predicate(r.url())&&r.request().method()==='POST'),action()]);if(!response.ok())throw new Error(await response.text());const queued=await response.json();for(let i=0;i<150;i++){const job=await get('/api/jobs/'+queued.id);if(job.status==='failed')throw new Error(job.error);if(job.status==='completed')return get('/api/state');await page.waitForTimeout(200)}throw new Error('Worker did not finish '+queued.id)};
async function snap(name){await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:path.join(output,name+'.png'),fullPage:true})}
let video;
try{
 await page.goto(base+'/#workspace');await page.getByRole('button',{name:'Enter workspace'}).click();await page.getByRole('heading',{name:/Your close/}).waitFor();
 let state=await get('/api/state');
 if(!state.uploads.length){
  await page.getByRole('navigation',{name:'Data workspace'}).getByRole('button',{name:'Upload files',exact:true}).click();
  const directory='../backend/fixtures/defective';
  await page.locator('input[type=file]').setInputFiles(fs.readdirSync(directory).filter(n=>n.endsWith('.csv')).map(n=>path.join(directory,n)));
  await page.waitForFunction(()=>document.querySelectorAll('.files-panel tbody tr').length===9);
  checks.push('CSV collection upload');
 }
 state=await get('/api/state');const missing=state.uploads.flatMap(u=>u.sheets).filter(s=>!s.mapping).length;
 if(missing){
  await page.getByRole('navigation',{name:'Data workspace'}).getByRole('button',{name:'Review mappings',exact:true}).click();
  for(let i=0;i<missing;i++){const role=await page.locator('.mapping-form select').first().inputValue();const heading=await page.locator('.mapping-editor h2').first().textContent();if(heading.includes('missing-budget')&&role!=='budgets')await page.getByLabel('Data role',{exact:true}).selectOption('budgets');await page.getByRole('button',{name:'Confirm mapping',exact:true}).click();await page.waitForTimeout(220)}
 }
 await page.getByRole('navigation',{name:'Data workspace'}).getByRole('button',{name:'Close Inbox',exact:true}).click();
 state=await runJob(url=>url.endsWith('/api/validate'),()=>page.getByRole('button',{name:'Run validation',exact:true}).click());await page.waitForTimeout(600);
 for(const code of ['invalid_row','duplicate_key','missing_budget']){
  const issue=state.validation.issues.find(i=>i.code===code&&i.status==='open');if(!issue)continue;
  if(code==='missing_budget'){
   // Supply the genuinely missing golden budget, rather than silently treating it as zero.
   const csv='month,department,account,amount,budget_version,currency\n2026-11,Operations,OTHER,50000,FY27 Approved,INR\n';
   await page.getByRole('navigation',{name:'Data workspace'}).getByRole('button',{name:'Upload files',exact:true}).click();
   await page.locator('input[type=file]').setInputFiles({name:'missing-budget.csv',mimeType:'text/csv',buffer:Buffer.from(csv)});
   await page.locator('.file-cell strong').filter({hasText:'missing-budget.csv'}).waitFor();await page.getByRole('navigation',{name:'Data workspace'}).getByRole('button',{name:'Review mappings',exact:true}).click();
   await page.getByLabel('Data role',{exact:true}).selectOption('budgets');
   await page.getByRole('button',{name:'Confirm mapping',exact:true}).click();
   await page.getByRole('navigation',{name:'Data workspace'}).getByRole('button',{name:'Close Inbox',exact:true}).click();
   state=await runJob(url=>url.endsWith('/api/validate'),()=>page.getByRole('button',{name:'Run validation',exact:true}).click());await page.waitForTimeout(600);continue;
  }
  await page.getByRole('button',{name:new RegExp('^'+issue.title)}).click();
  if(code==='invalid_row')await page.getByLabel('Reviewed value',{exact:true}).fill('2026-06-07');
  await page.getByLabel('Review reason',{exact:true}).fill('Synthetic demo: checked the original source row.');
  state=await runJob(url=>url.endsWith('/resolve'),()=>page.getByRole('button',{name:'Save decision & revalidate',exact:true}).click());await page.getByRole('dialog').waitFor({state:'hidden'});await page.waitForTimeout(600);
 }
 if(state.validation.blockers)throw new Error(JSON.stringify(state.validation.issues.filter(i=>i.severity==='blocker'&&i.status==='open')));
 await snap('complete-reconciled-close');state=await runJob(url=>url.endsWith('/api/snapshots'),()=>page.getByRole('button',{name:'Confirm reconciled data',exact:true}).click());await page.waitForTimeout(700);
 const sid=state.snapshots.at(-1).id;checks.push('Mapping, planted issue resolution, true missing budget, and sealed close');
 await page.getByRole('navigation',{name:'Financial analysis'}).getByRole('button',{name:'Financial overview',exact:true}).click();
 await page.getByLabel('Analysis snapshot',{exact:true}).selectOption(sid);await page.getByText('₹10,00,000',{exact:true}).first().waitFor();
 await page.locator('.fact-table').getByRole('button',{name:/Cloud infrastructure/}).click();await page.locator('.p2-driver-list').getByRole('button',{name:/^Usage/}).click();await page.getByRole('dialog',{name:'Variance evidence'}).waitFor();await snap('complete-variance-evidence');await page.getByRole('button',{name:'Close evidence',exact:true}).click();
 await page.getByRole('navigation',{name:'Financial analysis'}).getByRole('button',{name:'Scenario Lab',exact:true}).click();await page.getByRole('slider',{name:'Vendor spending reduction'}).fill('2000');await page.getByRole('checkbox',{name:'Freeze POS-001',exact:true}).check();await page.getByText('₹33.00L',{exact:true}).first().waitFor();
 await page.getByRole('textbox',{name:'Scenario name'}).fill('Complete build synthetic plan');await page.getByRole('button',{name:'Save scenario',exact:true}).click();await page.getByText('Scenario saved with its exact assumptions.',{exact:true}).waitFor();
 checks.push('Golden variance evidence and timing-aware saved scenario');
 await page.getByRole('navigation',{name:'Reports'}).getByRole('button',{name:'Report Studio',exact:true}).click();
 if(!await page.getByLabel('Report source snapshot',{exact:true}).isVisible())await page.getByRole('button',{name:'New report',exact:true}).click();
 await page.getByLabel('Report source snapshot',{exact:true}).selectOption(sid);
 const saved=await get(`/api/phase2/snapshots/${sid}/scenarios`);const scenario=saved.find(s=>s.name==='Complete build synthetic plan');
 await page.getByLabel('Report scenario',{exact:true}).selectOption(scenario.id);await page.getByLabel('Report generation mode',{exact:true}).selectOption('template');await page.getByLabel('Report title',{exact:true}).fill('Synthetic end-to-end management report');
 const [created]=await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/reports')&&r.request().method()==='POST'),page.getByRole('button',{name:'Generate draft',exact:true}).click()]);const reportId=(await created.json()).id;await page.waitForFunction(id=>document.querySelector('.studio-grid')?.getAttribute('data-report-id')===id,reportId,{timeout:30000});await snap('complete-report-draft');
 await page.locator('.report-preview .report-claim').first().click();await page.getByRole('dialog',{name:'Report claim evidence'}).waitFor();await page.getByRole('button',{name:'Close report evidence'}).click();
 await page.getByRole('tab',{name:'Edit draft',exact:true}).click();await page.getByRole('navigation',{name:'Report outline'}).getByRole('button',{name:/Proposed actions/}).click();await page.getByLabel('Action owner 1',{exact:true}).fill('Synthetic finance review team');await page.getByRole('button',{name:'Save revision',exact:true}).click();
 await page.getByText('Draft revision saved and validated.',{exact:true}).waitFor();
 await page.getByRole('button',{name:'Send for review',exact:true}).click();await page.getByRole('button',{name:'Review and approve',exact:true}).click();
 const approval=page.getByRole('dialog',{name:'Approve report revision'});for(const checkbox of await approval.getByRole('checkbox').all())await checkbox.check();
 await page.getByLabel('Approval review decision',{exact:true}).fill('Synthetic browser acceptance demonstration only: checked figures, wording, unknowns, sources, and cash assumptions. Not a real-company financial approval.');await approval.getByRole('button',{name:/Approve revision/}).click();
 await page.getByRole('heading',{name:'An exact version, approved.',exact:true}).waitFor();await snap('complete-approved-pack');
 const downloaded=page.waitForEvent('download');await page.getByRole('button',{name:'Download PDF',exact:true}).click();await (await downloaded).saveAs(path.join(output,'browser-approved-sample.pdf'));
 const approved=await get('/api/reports/'+reportId),rev=approved.version.revision;
 const original=await page.request.get(base+`/api/reports/${reportId}/export/pdf`);const originalBytes=await original.body();if(!original.ok()||originalBytes.subarray(0,4).toString()!=='%PDF')throw new Error('Approved API PDF was invalid');if(!fs.readFileSync(path.join(output,'browser-approved-sample.pdf')).equals(originalBytes))throw new Error('Browser download did not match the approved PDF');
 const note=await page.request.post(base+`/api/phase2/snapshots/${sid}/notes`,{data:{fact_id:approved.version.packet.facts[0].id,owner:'Synthetic regression fixture',text:'Review additional support after the prior approval.',reviewed:true}});if(!note.ok())throw new Error(await note.text());
 const stale=await get('/api/reports/'+reportId);if(stale.version.effective_state!=='stale')throw new Error('Changed included context did not mark approval stale');
 if((await page.request.get(base+`/api/reports/${reportId}/export/pdf`)).status()!==409)throw new Error('Stale current export was allowed');
 const historical=await page.request.get(base+`/api/reports/${reportId}/versions/${rev}/export/pdf?historical=true`);if(!Buffer.from(await historical.body()).equals(Buffer.from(originalBytes)))throw new Error('Historical approved PDF changed');
 checks.push('Linked report claims, owner assignment, explicit human-review form, frozen PDF, stale blocking, identical historical export');
 await page.reload();await page.getByRole('heading',{name:'A preserved historical approval.',exact:true}).waitFor();await snap('complete-stale-history');
 await page.setViewportSize({width:390,height:844});await snap('complete-mobile-pack');
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
 fs.writeFileSync(path.join(output,'browser-complete.json'),JSON.stringify({errors,mobileHorizontalOverflow:overflow,checks,snapshot_id:sid,report_id:reportId,approved_revision:rev},null,2));
 if(errors.length||overflow)throw new Error(JSON.stringify({errors,overflow}));
 console.log('Complete upload-to-approved-export and stale-history browser flow passed.');
}catch(error){await snap('complete-failure');fs.writeFileSync(path.join(output,'browser-failure.json'),JSON.stringify({message:String(error),errors,url:page.url()},null,2));throw error}finally{
 await context.tracing.stop({path:path.join(output,'complete-demo-trace.zip')});video=page.video();await context.close();if(video)fs.copyFileSync(await video.path(),path.join(output,'demo-recording.webm'));await browser.close();
}
