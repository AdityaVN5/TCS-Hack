import { chromium } from '@playwright/test';
import fs from 'node:fs';
const browser=await chromium.launch({channel:'chrome',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1050},deviceScaleFactor:1});
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
await page.goto('http://127.0.0.1:5173');
await page.getByRole('button',{name:'Enter workspace'}).click();
await page.getByRole('heading',{name:'A clearer close starts here.'}).waitFor();
await page.getByRole('button',{name:'Upload files',exact:true}).click();
const state=await page.request.get('http://127.0.0.1:5173/api/state').then(r=>r.json());
if(state.uploads.length===0){
 await page.getByRole('button',{name:'Try with issues'}).click();
 await page.getByRole('button',{name:'Review mappings',exact:true}).last().waitFor({state:'visible'});
 await page.waitForTimeout(500);
}
const current=await page.request.get('http://127.0.0.1:5173/api/state').then(r=>r.json());
const unmapped=current.uploads.flatMap(u=>u.sheets).filter(s=>!s.mapping).length;
if(unmapped){
 await page.getByRole('navigation').getByRole('button',{name:'Review mappings',exact:true}).click();
 for(let i=0;i<unmapped;i++){
  await page.getByRole('button',{name:'Confirm mapping',exact:true}).click();
  await page.waitForTimeout(220);
 }
}
await page.getByRole('button',{name:'Run validation',exact:true}).click();
await page.waitForTimeout(2000);
await page.getByRole('button',{name:/^Close Inbox/}).first().click();
await page.waitForTimeout(800);
await page.screenshot({path:'../artifacts/close-inbox.png',fullPage:true});
if(await page.getByRole('button',{name:/Source row needs attention/}).count()){
 await page.getByRole('button',{name:/Source row needs attention/}).click();
 await page.getByRole('heading',{name:'Original source row'}).waitFor();
 await page.screenshot({path:'../artifacts/source-review.png',fullPage:true});
 await page.getByRole('button',{name:'Close issue',exact:true}).click();
}
await page.getByRole('button',{name:'Workspace',exact:true}).click();
if(await page.locator('body').evaluate(el=>/[\u00c2\u00c3]|\u00e2[\u20ac\u201a\u0152\u2013]/.test(el.textContent||'')))throw new Error('Interface text has an encoding error');
await page.screenshot({path:'../artifacts/workspace-desktop.png',fullPage:true});
await page.setViewportSize({width:390,height:844});
await page.screenshot({path:'../artifacts/workspace-mobile.png',fullPage:true});
const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);
await page.getByRole('button',{name:'Upload files',exact:true}).click();
await page.getByRole('heading',{name:'Good data. Great beginnings.'}).waitFor();
await page.screenshot({path:'../artifacts/uploads-mobile.png',fullPage:true});
await page.setViewportSize({width:1440,height:1050});
await page.getByRole('navigation').getByRole('button',{name:'Close Inbox',exact:true}).click();
async function waitWorker(){
 for(let n=0;n<60;n++){
  const s=await page.request.get('http://127.0.0.1:5173/api/state').then(r=>r.json());
  if(!s.jobs.some(j=>['queued','running'].includes(j.status)))return s;
  await page.waitForTimeout(200);
 }
 throw new Error('Worker did not finish');
}
let s=await waitWorker();
for(const code of ['invalid_row','duplicate_key','missing_budget']){
 const issue=s.validation.issues.find(i=>i.code===code&&i.status==='open');
 if(!issue)continue;
 await page.getByRole('button',{name:new RegExp('^'+issue.title)}).click();
 if(code==='invalid_row')await page.getByLabel('Reviewed value',{exact:true}).fill('2026-06-07');
 await page.getByLabel('Review reason',{exact:true}).fill('Browser acceptance check: reviewed original source');
 await page.getByRole('button',{name:'Save decision & revalidate',exact:true}).click();
 await page.getByRole('dialog').waitFor({state:'hidden'});
 s=await waitWorker();
 await page.waitForTimeout(900);
}
if(s.validation.blockers)throw new Error('Corrections left blocking issues');
await page.getByRole('button',{name:'Confirm reconciled data',exact:true}).click();
s=await waitWorker();
if(!s.snapshots.length)throw new Error('No sealed snapshot');
await page.getByRole('heading',{name:'Sealed versions',exact:true}).waitFor();
await page.screenshot({path:'../artifacts/confirmed-snapshot.png',fullPage:true});
await page.getByRole('button',{name:'Browse records',exact:true}).first().click();
await page.getByLabel('Snapshot data role',{exact:true}).selectOption('actuals');
await page.getByText('24 accepted records',{exact:true}).waitFor();
await page.getByRole('button',{name:'Next',exact:true}).click();
await page.getByText('21–24 of 24',{exact:true}).waitFor();
await page.getByRole('button',{name:/Inspect record/}).first().click();
await page.getByRole('heading',{name:'Original source values',exact:true}).waitFor();
await page.locator('.source-comparison .source-values').first().waitFor();
const href=await page.getByRole('link',{name:'Original file',exact:true}).getAttribute('href');
const original=await page.request.get('http://127.0.0.1:5173'+href);
if(!original.ok()||!(await original.text()).includes('journal_id'))throw new Error('Original snapshot source was not downloadable');
await page.screenshot({path:'../artifacts/snapshot-source.png',fullPage:true});
await page.getByRole('button',{name:'Close snapshot records',exact:true}).click();
fs.writeFileSync('../artifacts/browser-check.json',JSON.stringify({errors,mobileHorizontalOverflow:overflow,flows:['login','demo import or existing draft','mapping confirmations','worker validation','source review','desktop workspace','mobile navigation','correction decisions','snapshot confirmation','snapshot pagination and role filter','sealed original-source download']},null,2));
await browser.close();
if(errors.length||overflow)throw new Error(JSON.stringify({errors,overflow}));
console.log('Browser flow passed, no runtime errors or mobile horizontal overflow.');
