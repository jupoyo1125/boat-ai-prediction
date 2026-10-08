const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const definitions = script.slice(0, script.lastIndexOf("document.getElementById('date').addEventListener"));

function context(outcomes, gate, analysisHandler) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], {textContent:'-', innerHTML:'', value:'', disabled:false}]));
  elements.get('date').value = '20261007';
  elements.get('stadium').value = '10';
  elements.get('stadium').options = [{value:'10', textContent:'三国'}];
  elements.get('stadium').selectedIndex = 0;
  let calls = 0;
  const requests=[];
  const ctx = vm.createContext({
    document:{getElementById(id){return elements.get(id);}},
    URLSearchParams,
    console:{error(){}},
    setTimeout(resolve){resolve();},
    async fetch(url, options) {
      if(url === '/api/analyze_batch') {
        calls++;
        const races=JSON.parse(options.body).races;
        requests.push({url,races});
        const results=analysisHandler ? analysisHandler(races) : races.map(race=>({race,...outcomes[race-1].body}));
        return {ok:true,status:200,json:async()=>({ok:true,results})};
      }
      if(url === '/api/settle_batch') {
        calls++;
        const races=JSON.parse(options.body).races;
        requests.push({url,races});
        if(gate && races.includes(1)) await gate;
        const selected=races.map(race=>({...outcomes[race-1].body,race}));
        const disconnected=races.map(race=>outcomes[race-1]).find(x=>x.throw);
        if(disconnected) throw new Error(disconnected.throw);
        return {ok:true,status:200,json:async()=>({ok:true,results:selected})};
      }
      if(url==='/api/performance') {
        return {ok:true,json:async()=>({ok:true,category_stats:{},legacy_records:0})};
      }
      if(url==='/api/model') return {ok:true,json:async()=>({ok:true,model:{}})};
      throw new Error('Unexpected request '+url);
    },
  });
  vm.runInContext(definitions,ctx);
  return {ctx,elements,requests,calls:()=>calls};
}
const settled={status:200,body:{ok:true,status:'settled'}};
const failure={status:502,body:{ok:false,code:'official_unavailable',error:'公式サイトとの通信に失敗しました。'}};

async function run() {
  const allFailed=context(Array(12).fill(failure));
  await vm.runInContext('batchSettle(false)',allFailed.ctx);
  assert.equal(allFailed.calls(),2);
  assert.equal(Number(allFailed.elements.get('batchFailed').textContent),12);
  assert.equal(Number(allFailed.elements.get('batchSuccess').textContent),0);
  assert.equal(allFailed.elements.get('batchProgress').textContent,'12 / 12');
  assert.ok(allFailed.elements.get('batchStatus').textContent.startsWith('⚠️'));
  assert.ok(allFailed.elements.get('batchStatus').textContent.includes('公式サイトとの通信'));
  console.log('PASS: Twelve failures show twelve, with their cause and no success badge.');

  const allSuccess=context(Array(12).fill(settled));
  await vm.runInContext('batchSettle(false)',allSuccess.ctx);
  assert.equal(Number(allSuccess.elements.get('batchSuccess').textContent),12);
  assert.equal(Number(allSuccess.elements.get('batchFailed').textContent),0);
  console.log('PASS: Final successful race is included in the success count.');

  const already=context(Array(12).fill({status:200,body:{ok:true,status:'already_settled'}}));
  await vm.runInContext('batchSettle(false)',already.ctx);
  assert.equal(Number(already.elements.get('batchSuccess').textContent),12);
  assert.equal(Number(already.elements.get('batchFailed').textContent),0);
  assert.ok(already.elements.get('batchStatus').textContent.includes('取得・学習済み：12'));
  assert.ok(already.elements.get('batchStatus').textContent.includes('新たに学習：0'));
  console.log('PASS: Repeated runs show successful checks without implying new learning.');

  const mixed=context([
    settled,{status:200,body:{ok:true,status:'already_settled'}},
    {status:404,body:{ok:false,code:'prediction_missing'}},
    {status:404,body:{ok:false,code:'result_pending'}},
    failure,{status:404,body:{ok:false,error:'Unknown failure'}},
    ...Array(6).fill(settled),
  ]);
  await vm.runInContext('batchSettle(false)',mixed.ctx);
  const status=mixed.elements.get('batchStatus').textContent;
  assert.equal(Number(mixed.elements.get('batchSuccess').textContent),8);
  assert.equal(Number(mixed.elements.get('batchFailed').textContent),2);
  for(const text of ['新たに学習：7','取得・学習済み：1','予想なし：1','結果待ち：1','失敗：2','確認：12']) {
    assert.ok(status.includes(text),text);
  }
  console.log('PASS: Missing prediction, pending result, already learned, and failures are distinct.');

  const network=context([...Array(11).fill(settled),{throw:'Network disconnected'}]);
  await vm.runInContext('batchSettle(false)',network.ctx);
  assert.equal(Number(network.elements.get('batchSuccess').textContent),6);
  assert.equal(Number(network.elements.get('batchFailed').textContent),6);
  assert.ok(network.elements.get('batchStatus').textContent.includes('Network disconnected'));
  console.log('PASS: Disconnected batch keeps earlier successes and marks its unconfirmed races as failures.');

  let release;
  const gate=new Promise(resolve=>release=resolve);
  const busy=context(Array(12).fill(settled),gate);
  const first=vm.runInContext('batchSettle(false)',busy.ctx);
  for(const id of ['batchAnalyzeBtn','batchAnalyzeAllBtn','batchSettleBtn','batchSettleAllBtn']) {
    assert.equal(busy.elements.get(id).disabled,true);
  }
  await vm.runInContext('batchSettle(true)',busy.ctx);
  await vm.runInContext('batchAnalyze(false)',busy.ctx);
  assert.equal(busy.calls(),1);
  release();
  await first;
  assert.equal(busy.calls(),2);
  for(const id of ['batchAnalyzeBtn','batchAnalyzeAllBtn','batchSettleBtn','batchSettleAllBtn']) {
    assert.equal(busy.elements.get(id).disabled,false);
  }
  console.log('PASS: Overlapping batches are blocked and buttons become usable after completion.');

  const analyze=context(Array(12).fill({body:{ok:true,status:'saved'}}));
  await vm.runInContext('batchAnalyze(false)',analyze.ctx);
  assert.equal(analyze.calls(),4);
  assert.equal(Number(analyze.elements.get('batchSuccess').textContent),12);
  assert.equal(Number(analyze.elements.get('batchFailed').textContent),0);
  assert.ok(analyze.requests.every(r=>r.races.length===3));
  console.log('PASS: Analysis saves twelve races in four requests and counts only confirmed saves.');

  let retried=false;
  const retry=context([],null,races=>races.map(race=>{
    if(race===1) return {race,ok:false,code:'prediction_frozen',error:'保存済みの自動予想'};
    if(race===2 && !retried){retried=true;return {race,ok:false,code:'analysis_failed',error:'一時的な通信障害'};}
    return {race,ok:true,status:'saved'};
  }));
  await vm.runInContext('batchAnalyze(false)',retry.ctx);
  assert.deepEqual(retry.requests[1].races,[2]);
  assert.equal(retry.calls(),5);
  assert.equal(Number(retry.elements.get('batchSuccess').textContent),11);
  assert.equal(Number(retry.elements.get('batchFailed').textContent),1);
  console.log('PASS: Analysis retries only failed races; frozen and successful races are excluded.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
