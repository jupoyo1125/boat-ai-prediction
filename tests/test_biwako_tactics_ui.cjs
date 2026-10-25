const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const source=html.split('<script>')[1].split('</script>')[0];
new vm.Script(source);
const definitions=source.slice(0,source.lastIndexOf("document.getElementById('date').addEventListener"));

function createEnvironment(fetcher){
  const elements=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[
    match[1],{value:'',hidden:false,disabled:false,textContent:'',innerHTML:''}
  ]));
  elements.get('date').value='20261026';
  elements.get('stadium').value='11';
  elements.get('race').value='1R';
  elements.get('fixed').value='none';
  const calls=[];
  const sandbox=vm.createContext({console,Date,URLSearchParams,AbortController,setTimeout,clearTimeout,
    document:{getElementById:id=>elements.get(id)},
    fetch:async (url,options)=>{
      calls.push({url,options});
      return fetcher(url,options);
    }});
  vm.runInContext(definitions,sandbox);
  return {elements,calls,run:script=>vm.runInContext(script,sandbox)};
}

const response=(payload,status=200)=>({ok:status<400,status,json:async()=>payload});
function row(boat){
  return {boat,name:boat===1?'<b>実名</b>':'選手'+boat,course:boat,
    provisional_course:false,starts:50,baseline_starts:100,
    escape:boat===1?56:null,passed:boat===1?20:null,swept:boat===1?10:null,
    let_escape:boat>1?50:null,sashi:boat>1?12:null,makuri:boat>1?9:null,
    rates:{first:{base:55,estimated:58,delta:3},
           second:{base:20,estimated:18,delta:-2},
           third:{base:12,estimated:null,delta:null}}};
}
const ready={ok:true,code:'ready',ready_days:170,total_days:365,provisional:true,
             boats:Array.from({length:6},(_,i)=>row(i+1))};
async function test(){
  let payload={ok:true,code:'preparing',ready_days:5,total_days:365,boats:[]};
  const env=createEnvironment(async()=>response(payload));
  await env.run('loadBiwakoTactics()');
  assert.equal(env.elements.get('biwakoTacticsPanel').hidden,false);
  assert.match(env.elements.get('biwakoTacticsStatus').textContent,/集計中.*5\/365/);
  assert.equal(env.elements.get('biwakoTacticsResults').hidden,true);
  assert.equal(env.calls.length,1);
  await env.run('loadBiwakoTactics()');
  assert.equal(env.calls.length,1,'same race uses 60-second cache');

  payload=ready;
  await env.run('loadBiwakoTactics(true)');
  assert.equal(env.elements.get('biwakoTacticsResults').hidden,false);
  const markup=env.elements.get('biwakoTacticsRows').innerHTML;
  assert.match(markup,/逃げ.*56.0%/);
  assert.match(markup,/逃がし.*50.0%/);
  assert.match(markup,/58.0%/);
  assert.match(markup,/↑\+3.0pt/);
  assert.match(markup,/↓-2.0pt/);
  assert.match(markup,/補正不可/);
  assert.doesNotMatch(markup,/<b>実名<\/b>/);
  assert.match(markup,/&lt;b&gt;実名&lt;\/b&gt;/);
  assert.match(env.elements.get('biwakoTacticsStatus').textContent,/暫定値/);

  env.elements.get('stadium').value='04';
  await env.run('loadBiwakoTactics()');
  assert.equal(env.elements.get('biwakoTacticsPanel').hidden,true);
  assert.equal(env.calls.length,2,'non-Biwako courses do not call the tactical API');

  let oldResolve;
  const pending=new Promise(resolve=>oldResolve=resolve);
  const delayed=createEnvironment(async url=>{
    if(url.includes('race=1'))return pending;
    return response(ready);
  });
  const old=delayed.run('loadBiwakoTactics()');
  delayed.elements.get('race').value='2R';
  await delayed.run('loadBiwakoTactics()');
  oldResolve(response(ready));
  await old;
  assert.equal(delayed.calls.length,2);
  assert.equal(delayed.calls[0].options.signal.aborted,true);
  assert.match(delayed.elements.get('biwakoTacticsStatus').textContent,/170\/365/);
  console.log('PASS: Biwako-only race panel shows honest pending, sourced rates, change arrows, escaped names, and race-scoped requests.');
}
test().catch(error=>{console.error(error);process.exitCode=1;});
