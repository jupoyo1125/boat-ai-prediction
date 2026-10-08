const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const definitions = script.slice(0, script.lastIndexOf("document.getElementById('date').addEventListener"));
const bindings = script.slice(script.lastIndexOf("document.getElementById('date').addEventListener"), script.lastIndexOf('setupDates();'));
const prediction = {
  ok:true, venue:'三国', main:1, second:2, hole:3, scenario:'逃げ',
  boats:Array.from({length:6},(_,i)=>({boat:i+1, name:'選手', score:50})),
  features:{}, bets:[{bet:'1-2-3',category:'gachi',odds:10,probability:.1,ev:1}],
};
const saved = {
  prediction_origin:'automatic', prediction, prediction_saved_at:'2026-10-08T10:00:00+09:00',
  combo:'123', bets:prediction.bets, features:{}, predicted_first:1, learned:false,
};
const response = (data,status=200)=>({ok:status<400,status,json:async()=>data});

function context(handler) {
  const nodes=[];
  function node() {
    const result={textContent:'-',innerHTML:'',value:'',disabled:false,children:[],listeners:{},
      addEventListener(name,callback){this.listeners[name]=callback;},
      classList:{add(){},remove(){}},appendChild(child){this.children.push(child);}};
    nodes.push(result); return result;
  }
  const elements=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],node()]));
  Object.assign(elements.get('date'),{value:'20261008'});
  Object.assign(elements.get('stadium'),{value:'10',options:[{value:'10',textContent:'三国'}],selectedIndex:0});
  elements.get('race').value='1R'; elements.get('fixed').value='none';
  elements.get('perfScope').value='automatic';
  const calls=[];
  const ctx=vm.createContext({Date,URLSearchParams,console:{error(){}},
    window:{scrollTo(){}},setTimeout(resolve){resolve();},
    document:{getElementById:id=>elements.get(id),createElement:node,querySelectorAll:()=>nodes},
    async fetch(url,options){
      calls.push({url,options});
      const custom=handler && await handler(url,options);
      if(custom) return custom;
      if(url==='/api/performance') return response({ok:true,category_stats:{gachi:{roi:34.4,hit_rate:16.7}},automatic_category_stats:{gachi:{roi:99,hit_rate:100}},legacy_records:0});
      if(url==='/api/model') return response({ok:true,model:{samples:1,hits:1,weights:{}}});
      if(url.startsWith('/api/saved_prediction?')) return response({ok:true,record:saved});
      throw new Error('Unexpected request '+url);
    }});
  vm.runInContext(definitions,ctx);
  return {ctx,elements,calls,run:code=>vm.runInContext(code,ctx)};
}

async function run() {
  const automatic=context();
  await automatic.run('loadSelectedSavedPrediction()');
  assert.equal(automatic.elements.get('main').textContent,'1号艇');
  assert.equal(automatic.elements.get('analyzeBtn').disabled,true);
  assert.equal(automatic.elements.get('fixed').disabled,true);
  assert.equal(automatic.elements.get('settleBtn').disabled,false);
  assert.match(automatic.elements.get('betsGachi').innerHTML,/1-2-3/);
  assert.ok(automatic.calls.every(call=>!call.options?.method));
  console.log('PASS: Selecting an automatic forecast renders its saved bets without creating a new prediction.');

  const rates=context(); await rates.run('loadPerformanceStatus()');
  assert.equal(rates.elements.get('perfGachiRoi').textContent,'99.0%');
  rates.elements.get('perfScope').value='all'; await rates.run('loadPerformanceStatus()');
  assert.equal(rates.elements.get('perfGachiRoi').textContent,'34.4%');
  assert.match(rates.elements.get('perfNote').textContent,/以前の手動予想/);
  console.log('PASS: Automatic and historical manual performance use separate totals.');

  const empty=context(url=>url.includes('race=2') ? response({ok:false},404) : undefined);
  await empty.run('loadSelectedSavedPrediction()'); empty.elements.get('race').value='2R';
  await empty.run('loadSelectedSavedPrediction()');
  assert.equal(empty.elements.get('main').textContent,'-');
  assert.equal(empty.elements.get('betsGachi').innerHTML,'');
  assert.equal(empty.elements.get('analyzeBtn').disabled,false);
  assert.equal(empty.elements.get('fixed').disabled,false);
  assert.equal(empty.elements.get('settleBtn').disabled,true);
  assert.match(empty.elements.get('reason').textContent,/保存されると/);
  console.log('PASS: A race without a saved forecast clears the previous race and waits for exhibition.');

  const manual=context(url=>url.startsWith('/api/saved_prediction?') ? response({ok:true,record:{combo:'123',bets:prediction.bets,learned:false}}) : undefined);
  await manual.run('loadSelectedSavedPrediction()');
  assert.equal(manual.elements.get('settleBtn').disabled,false);
  assert.match(manual.elements.get('reason').textContent,/手動予想を保存済み/);
  console.log('PASS: Historical manual forecasts still allow result settlement.');

  let release;
  const gate=new Promise(resolve=>release=resolve);
  const late=context(async url=>{
    if(url.includes('race=1')) {await gate;return response({ok:true,record:saved});}
    if(url.includes('race=2')) return response({ok:true,record:{...saved,prediction:{...prediction,main:2}}});
  });
  const old=late.run('loadSelectedSavedPrediction()');late.elements.get('race').value='2R';
  await late.run('loadSelectedSavedPrediction()');release();await old;
  assert.equal(late.elements.get('main').textContent,'2号艇');
  console.log('PASS: A slow response for the previous selection cannot replace the selected race.');

  const frozen=context((url,options)=>{
    if(url.startsWith('/api/analyze?')) return response({...prediction,main:6});
    if(url==='/api/performance' && options?.method==='POST') return response({ok:false,code:'prediction_frozen'},409);
  });
  await frozen.run('analyze()');
  assert.equal(frozen.elements.get('main').textContent,'1号艇');
  assert.equal(frozen.elements.get('analyzeBtn').disabled,true);
  assert.equal(frozen.calls.filter(call=>call.options?.method==='POST').length,1);
  console.log('PASS: A manual-save conflict reloads the frozen automatic forecast without repeated writes.');

  const stale=context(url=>url==='/api/automation' ? response({ok:true,enabled:true,phase:'watching',date:'20261008',predicted:12,settled:3,pending:9,last_checked_at:new Date(Date.now()-11*60*1000).toISOString()}) : undefined);
  await stale.run('loadAutomationStatus()');
  assert.equal(stale.elements.get('autoPredicted').textContent,'12R');
  assert.equal(stale.elements.get('autoPhase').textContent,'確認が遅れています');
  assert.match(stale.elements.get('autoStatus').textContent,/締切を過ぎた/);
  console.log('PASS: Delayed monitoring is visible while saved totals remain readable.');

  const buttons=context();buttons.run(bindings);
  buttons.elements.get('race').value='2R';
  await buttons.elements.get('race').listeners.change();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(buttons.elements.get('race').value,'2R');
  assert.ok(buttons.calls.some(call=>call.url.includes('race=2')));
  console.log('PASS: The race selector loads the corresponding saved automatic forecast.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
