const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const definitions = script.slice(0, script.lastIndexOf("document.getElementById('date').addEventListener"));

function context(outcomes, gate) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], {textContent:'-', innerHTML:'', value:'', disabled:false}]));
  elements.get('date').value = '20261007';
  elements.get('stadium').value = '10';
  elements.get('stadium').options = [{value:'10', textContent:'三国'}];
  elements.get('stadium').selectedIndex = 0;
  let calls = 0;
  const ctx = vm.createContext({
    document:{getElementById(id){return elements.get(id);}},
    URLSearchParams,
    console:{error(){}},
    setTimeout(resolve){resolve();},
    async fetch(url, options) {
      if(url === '/api/settle_prediction') {
        calls++;
        const race=JSON.parse(options.body).race;
        if(gate && race===1) await gate;
        const outcome=outcomes[race-1];
        if(outcome.throw) throw new Error(outcome.throw);
        return {ok:outcome.status<400,status:outcome.status,json:async()=>outcome.body};
      }
      if(url==='/api/performance') {
        return {ok:true,json:async()=>({ok:true,category_stats:{},legacy_records:0})};
      }
      if(url==='/api/model') return {ok:true,json:async()=>({ok:true,model:{}})};
      throw new Error('Unexpected request '+url);
    },
  });
  vm.runInContext(definitions,ctx);
  return {ctx,elements,calls:()=>calls};
}
const settled={status:200,body:{ok:true,status:'settled'}};
const failure={status:502,body:{ok:false,code:'official_unavailable',error:'公式サイトとの通信に失敗しました。'}};

async function run() {
  const allFailed=context(Array(12).fill(failure));
  await vm.runInContext('batchSettle(false)',allFailed.ctx);
  assert.equal(allFailed.calls(),12);
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
  assert.equal(Number(network.elements.get('batchSuccess').textContent),11);
  assert.equal(Number(network.elements.get('batchFailed').textContent),1);
  assert.ok(network.elements.get('batchStatus').textContent.includes('Network disconnected'));
  console.log('PASS: Final network failure is counted and explained.');

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
  assert.equal(busy.calls(),12);
  for(const id of ['batchAnalyzeBtn','batchAnalyzeAllBtn','batchSettleBtn','batchSettleAllBtn']) {
    assert.equal(busy.elements.get(id).disabled,false);
  }
  console.log('PASS: Overlapping batches are blocked and buttons become usable after completion.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
