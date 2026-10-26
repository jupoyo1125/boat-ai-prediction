const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const source=html.split('<script>')[1].split('</script>')[0];
new vm.Script(source);
const definitions=source.slice(0,source.lastIndexOf("document.getElementById('date').addEventListener"));
let now=Date.parse('2026-10-26T09:00:00Z');
class Clock extends Date {
  static now(){return now;}
}
let predicted=10,refreshes=0,requests=0;
const elements=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[
  match[1],{textContent:'',value:'',hidden:false}
]));
const ctx=vm.createContext({Date:Clock,console,URLSearchParams,AbortController,setTimeout,clearTimeout,
  document:{getElementById:id=>elements.get(id)},
  fetch:async url=>{
    if(url!=='/api/automation')throw new Error('Unexpected URL '+url);
    requests++;
    return {ok:true,json:async()=>({
      ok:true,enabled:true,date:'20261026',phase:'watching',
      predicted,settled:2,pending:predicted-2,
      last_checked_at:new Date(now).toISOString()
    })};
  },
  recordRefresh(){refreshes++;},
});
vm.runInContext(definitions,ctx);
vm.runInContext('loadSelectedSavedPrediction=async()=>recordRefresh()',ctx);
(async()=>{
  await vm.runInContext('loadAutomationStatus()',ctx);
  assert.equal(refreshes,1,'first status gets saved forecast immediately');
  now+=30000;
  await vm.runInContext('loadAutomationStatus()',ctx);
  now+=30000;
  await vm.runInContext('loadAutomationStatus()',ctx);
  assert.equal(refreshes,1,'missing forecast is not refetched every 30-60 seconds');
  now+=60001;
  await vm.runInContext('loadAutomationStatus()',ctx);
  assert.equal(refreshes,2,'missing forecast retries after 2 minutes');
  predicted++;
  await vm.runInContext('loadAutomationStatus()',ctx);
  assert.equal(refreshes,3,'newly saved forecast counters trigger immediate update');
  vm.runInContext('lastPrediction={prediction_origin:"automatic"}',ctx);
  now+=180001;
  await vm.runInContext('loadAutomationStatus()',ctx);
  assert.equal(refreshes,3,'present forecast does not cause periodic saved reads');
  assert.equal(requests,6,'the test only reads status when explicitly requested');
  assert.match(source,/if\(!document\.hidden\) loadAutomationStatus\(\);\s*\},60000\);/);
  console.log('PASS: status refresh interval is 60s, empty saved forecasts retry every 2min, fresh forecasts trigger immediate updates.');
})().catch(err=>{console.error(err);process.exitCode=1;});
