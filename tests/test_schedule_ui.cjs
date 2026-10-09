const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0];
const definitions=script.slice(0,script.lastIndexOf("document.getElementById('date').addEventListener"));
const response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
const venues=[{stadium:'01',venue:'桐生'},{stadium:'10',venue:'三国'}];
const schedule=(date='20261009',extra={})=>({ok:true,date,venues,...extra});
const failed=()=>response({ok:false,error:'公式サイトへの通信が混み合っています。'},503);

function context(handler){
  function node(tag='div'){
    let markup='';
    const element={tag,textContent:'',value:'',disabled:false,hidden:false,children:[],style:{},
      classList:{add(){},remove(){}},addEventListener(){},
      appendChild(child){
        this.children.push(child);
        if(this.tag==='select' && this.children.length===1) this.value=child.value;
      }};
    Object.defineProperty(element,'innerHTML',{
      get:()=>markup,set(value){
        markup=value;this.children=[];
        if(this.tag==='select') this.value='';
      }});
    Object.defineProperty(element,'options',{get(){return this.children;}});
    Object.defineProperty(element,'selectedIndex',{get(){return this.children.findIndex(o=>o.value===this.value);}});
    return element;
  }
  const elements=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],node()]));
  for(const id of ['date','stadium','race','fixed']) elements.get(id).tag='select';
  elements.get('date').value='20261009';elements.get('race').value='1R';elements.get('fixed').value='none';
  const calls=[],timers=new Map();let nextTimer=0;
  const ctx=vm.createContext({Date,URLSearchParams,AbortController,console:{error(){}},
    window:{scrollTo(){}},
    setTimeout(callback,delay){
      const id=++nextTimer;
      if(delay===45000) timers.set(id,callback);
      else Promise.resolve().then(callback);
      return id;
    },clearTimeout(id){timers.delete(id);},
    document:{getElementById:id=>elements.get(id),createElement:node,querySelectorAll:()=>[]},
    async fetch(url,options){
      calls.push({url,options});
      const custom=await handler(url,options);
      if(custom) return custom;
      if(url.startsWith('/api/saved_prediction?')) return response({ok:false},404);
      throw new Error('Unexpected request '+url);
    }});
  vm.runInContext(definitions,ctx);
  return {ctx,elements,calls,timers,run:code=>vm.runInContext(code,ctx),
    scheduleCalls:()=>calls.filter(call=>call.url.startsWith('/api/schedule?'))};
}

async function run(){
  let attempts=0;
  const transient=context(url=>url.startsWith('/api/schedule?')
    ? (++attempts===1 ? failed() : response(schedule())) : null);
  await transient.run('loadSchedule()');
  assert.equal(transient.scheduleCalls().length,2);
  assert.equal(transient.elements.get('stadium').disabled,false);
  assert.equal(transient.elements.get('analyzeBtn').disabled,false);
  assert.equal(transient.elements.get('scheduleRetry').hidden,true);
  assert.equal(transient.elements.get('batchTarget').textContent,'2場・24R');
  console.log('PASS: A transient schedule failure recovers automatically and re-enables selection.');

  let offline=true;
  const manual=context(url=>url.startsWith('/api/schedule?') ? (offline?failed():response(schedule())) : null);
  await manual.run('loadSchedule()');
  assert.equal(manual.scheduleCalls().length,3);
  assert.equal(manual.elements.get('analyzeBtn').disabled,true);
  assert.equal(manual.elements.get('scheduleRetry').hidden,false);
  assert.match(manual.elements.get('scheduleNotice').textContent,/開催場を再取得/);
  offline=false;await manual.run('loadSchedule()');
  assert.equal(manual.elements.get('stadium').disabled,false);
  assert.equal(manual.elements.get('analyzeBtn').disabled,false);
  assert.match(html,/id="scheduleRetry"[^>]*onclick="loadSchedule\(\)"/);
  console.log('PASS: Persistent failures expose a working retry button without a page reload.');

  let refreshFails=false;
  const retained=context(url=>url.startsWith('/api/schedule?') ? (refreshFails?failed():response(schedule())) : null);
  await retained.run('loadSchedule()');retained.elements.get('stadium').value='10';
  refreshFails=true;await retained.run('loadSchedule()');
  assert.equal(retained.elements.get('stadium').value,'10');
  assert.equal(retained.elements.get('stadium').disabled,false);
  assert.match(retained.elements.get('scheduleNotice').textContent,/この日付の取得済み開催場/);
  retained.elements.get('date').value='20261008';await retained.run('loadSchedule()');
  assert.equal(retained.elements.get('stadium').value,'');
  assert.equal(retained.elements.get('stadium').disabled,true);
  assert.equal(retained.elements.get('analyzeBtn').disabled,true);
  console.log('PASS: A failed same-day refresh preserves selection; another date cannot reuse it.');

  let oldResolve,firstOptions;
  const late=context((url,options)=>{
    if(url==='/api/schedule?date=20261009'){
      firstOptions=options;return new Promise(resolve=>oldResolve=resolve);
    }
    if(url==='/api/schedule?date=20261008') return response(schedule('20261008',{venues:[{stadium:'14',venue:'鳴門'}]}));
  });
  const old=late.run('loadSchedule()');
  late.elements.get('date').value='20261008';await late.run('loadSchedule()');
  assert.equal(firstOptions.signal.aborted,true);
  oldResolve(response(schedule()));await old;
  assert.equal(late.elements.get('stadium').value,'14');
  assert.equal(late.elements.get('batchTarget').textContent,'1場・12R');
  console.log('PASS: A late schedule response cannot replace the newly selected date.');

  const mismatch=context(url=>url.startsWith('/api/schedule?') ? response(schedule('20261008')) : null);
  await mismatch.run('loadSchedule()');
  assert.equal(mismatch.elements.get('stadium').value,'');
  assert.equal(mismatch.elements.get('analyzeBtn').disabled,true);
  assert.equal(mismatch.scheduleCalls().length,3);
  console.log('PASS: A response for the wrong date is rejected even if its HTTP status is successful.');

  const stale=context(url=>url.startsWith('/api/schedule?') ? response(schedule('20261009',{stale:true,age_seconds:950})) : null);
  await stale.run('loadSchedule()');
  assert.equal(stale.elements.get('stadium').disabled,false);
  assert.match(stale.elements.get('scheduleNotice').textContent,/取得済み開催場/);
  assert.equal(stale.elements.get('scheduleRetry').hidden,false);
  console.log('PASS: A server fallback is marked as previously fetched data and offers refresh.');

  const empty=context(url=>url.startsWith('/api/schedule?') ? response(schedule('20261009',{venues:[]})) : null);
  await empty.run('loadSchedule()');
  assert.equal(empty.scheduleCalls().length,1);
  assert.equal(empty.elements.get('analyzeBtn').disabled,true);
  assert.equal(empty.elements.get('batchAnalyzeAllBtn').disabled,true);
  assert.match(empty.elements.get('scheduleNotice').textContent,/開催場はありません/);
  console.log('PASS: A day without races stays disabled and is not treated as a network error.');

  let timeoutAttempts=0;
  const timeout=context((url,options)=>{
    if(!url.startsWith('/api/schedule?')) return;
    if(++timeoutAttempts>1) return response(schedule());
    return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('timeout'))));
  });
  const pending=timeout.run('loadSchedule()');
  [...timeout.timers.values()][0]();await pending;
  assert.equal(timeout.scheduleCalls().length,2);
  assert.equal(timeout.elements.get('stadium').disabled,false);
  assert.equal(timeout.timers.size,0);
  console.log('PASS: An unresponsive schedule request is aborted and retried.');

  const invalid=context(url=>url.startsWith('/api/schedule?') ? response({ok:false,error:'日付を確認してください。'},400) : null);
  await invalid.run('loadSchedule()');
  assert.equal(invalid.scheduleCalls().length,1);
  assert.equal(invalid.elements.get('analyzeBtn').disabled,true);
  console.log('PASS: An invalid date is not repeatedly sent to the server.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
