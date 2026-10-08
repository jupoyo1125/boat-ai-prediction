const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const definitions = script.slice(0, script.lastIndexOf("document.getElementById('date').addEventListener"));
const historical = {ok:true,exists:true,start_date:'20161009',end_date:'20261008',cursor:'20161009',
  phase:'running',enabled:true,days_done:1,total_days:3652,progress:.03,samples:173,evaluated:0,
  missing_odds:173,missing_features:0,excluded_results:7,category_stats:{gachi:{roi:null,hit_rate:null}}};
const response = (data,status=200)=>({ok:status<400,status,json:async()=>data});
function context(handler){
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[match[1],
    {textContent:'-',innerHTML:'',value:'',disabled:false,style:{},classList:{add(){},remove(){}}}]));
  elements.get('perfScope').value='automatic';
  const calls=[];
  const ctx=vm.createContext({Date,URLSearchParams,console:{error(){}},
    document:{getElementById:id=>elements.get(id)},
    async fetch(url,options){
      calls.push({url,options});
      const custom=handler&&await handler(url,options);
      if(custom)return custom;
      if(url==='/api/performance')return response({ok:true,category_stats:{gachi:{roi:34.4,hit_rate:16.7}},
        automatic_category_stats:{gachi:{roi:99,hit_rate:100}},historical,legacy_records:0});
      if(url==='/api/historical')return response(historical);
      if(url==='/api/model')return response({ok:true,model:{samples:855,hits:262,weights:{}}});
      throw new Error('Unexpected request '+url);
    }});
  vm.runInContext(definitions,ctx);
  return {elements,calls,run:code=>vm.runInContext(code,ctx)};
}
async function run(){
  const live=context();await live.run('loadPerformanceStatus()');
  assert.equal(live.elements.get('perfGachiRoi').textContent,'99.0%');
  assert.equal(live.elements.get('historySamples').textContent,'173R');
  live.elements.get('perfScope').value='historical';await live.run('loadPerformanceStatus()');
  assert.equal(live.elements.get('perfGachiRoi').textContent,'-');
  assert.equal(live.elements.get('perfGachiHitRate').textContent,'-');
  assert.match(live.elements.get('perfNote').textContent,/当時保存した運用実績とは異なります/);
  console.log('PASS: Historical learning is visible without counting missing odds as losses or live performance.');

  const paused=context();paused.run(`renderHistorical(${JSON.stringify({...historical,enabled:false,phase:'paused'})})`);
  assert.equal(paused.elements.get('historyStart').disabled,false);
  assert.match(paused.elements.get('historyStart').textContent,/保存位置から再開/);
  assert.equal(paused.elements.get('historyPause').disabled,true);
  assert.equal(paused.elements.get('historyApply').disabled,true);
  console.log('PASS: Paused work can resume and cannot apply an incomplete model.');

  const completed=context();completed.run(`renderHistorical(${JSON.stringify({...historical,enabled:false,phase:'completed',samples:1000})})`);
  assert.equal(completed.elements.get('historyStart').disabled,true);
  assert.equal(completed.elements.get('historyApply').disabled,false);
  completed.run(`renderHistorical(${JSON.stringify({...historical,enabled:false,phase:'completed',samples:1000,model_applied_at:'2026-10-09'})})`);
  assert.equal(completed.elements.get('historyApply').disabled,true);
  assert.match(completed.elements.get('historyApply').textContent,/適用済み/);
  console.log('PASS: Applying a completed model requires an explicit button and shows its completed state.');

  const fail=context((url,options)=>url==='/api/historical'&&options?.method==='POST' ? response({ok:false,error:'通信エラー'},502):undefined);
  await fail.run("controlHistorical('start')");
  assert.equal(fail.calls.filter(call=>call.options?.method==='POST').length,1);
  assert.equal(fail.elements.get('historyPause').disabled,false);
  assert.match(fail.elements.get('historyStatus').textContent,/通信エラー/);
  console.log('PASS: A failed control response checks saved status without blindly starting another run.');

  const polling=context();polling.elements.get('perfScope').value='historical';await polling.run('loadHistoricalStatus()');
  assert.equal(polling.calls.length,1);
  assert.equal(polling.calls[0].url,'/api/historical');
  assert.equal(polling.elements.get('perfGachiRoi').textContent,'-');
  console.log('PASS: Progress refresh avoids reading the entire live ledger.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
