const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const source=html.split('<script>')[1].split('</script>')[0];
new vm.Script(source);
const definitions=source.slice(0,source.lastIndexOf("document.getElementById('date').addEventListener"));
const response=(data,status=200)=>({ok:status<400,json:async()=>data});
const result=(date='20261017',stadium='04',settled=[1,10],cancelled=[3])=>
  ({ok:true,date,stadium,settled_races:settled,cancelled_races:cancelled});
function context(handler){
  let now=new Date('2026-10-17T12:27:00Z').getTime();
  class Clock extends Date{static now(){return now;}}
  function element(){
    const attributes={},classes=new Set();let markup='';
    const el={value:'',textContent:'',disabled:false,children:[],hidden:false,style:{},classes,
      classList:{add:value=>classes.add(value),remove:value=>classes.delete(value)},
      setAttribute:(key,value)=>attributes[key]=String(value),getAttribute:key=>attributes[key]??null,
      addEventListener(){},appendChild(child){this.children.push(child);}};
    Object.defineProperty(el,'innerHTML',{get:()=>markup,set:value=>{markup=value;el.children=[];}});
    return el;
  }
  const elements=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[match[1],element()]));
  elements.get('date').value='20261017';elements.get('stadium').value='04';
  elements.get('race').value='1R';elements.get('fixed').value='none';
  const calls=[],timers=new Map();let next=0;
  const ctx=vm.createContext({Date:Clock,URLSearchParams,AbortController,console:{error(){}},
    window:{scrollTo(){}},
    setTimeout(callback,delay){const id=++next;timers.set(id,{callback,delay});return id;},
    clearTimeout:id=>timers.delete(id),
    document:{getElementById:id=>elements.get(id),createElement:element,querySelectorAll(selector){
      if(selector==='.venue-tile') return elements.get('raceList').children;
      if(selector==='.race-btn') return elements.get('raceButtons').children;
      return [];
    }},
    async fetch(url,options){
      calls.push({url,options});
      if(handler){const custom=await handler(url,options);if(custom)return custom;}
      if(url.startsWith('/api/race_status?')){
        const params=new URLSearchParams(url.split('?')[1]);return response(result(params.get('date'),params.get('stadium')));
      }
      if(url.startsWith('/api/saved_prediction?'))return response({ok:false},404);
      throw Error('Unexpected request '+url);
    }});
  vm.runInContext(definitions,ctx);
  return {elements,calls,timers,run:code=>vm.runInContext(code,ctx),advance:ms=>now+=ms,
    race:number=>elements.get('raceButtons').children[number-1],
    statusCalls:()=>calls.filter(call=>call.url.startsWith('/api/race_status?'))};
}

async function main(){
  const view=context();
  view.run("renderAllRaces([{stadium:'04',venue:'平和島',meeting_day_label:'5日目'},{stadium:'16',venue:'児島',meeting_day_label:'初日'},{stadium:'24',venue:'大村',meeting_day_label:'優勝戦'},{stadium:'01',venue:'桐生'}])");
  const tiles=view.elements.get('raceList').children;
  assert.equal(tiles.length,24);
  assert.equal(tiles[3].children[0].textContent,'5日目');
  assert.equal(tiles[15].children[0].textContent,'初日');
  assert.equal(tiles[23].children[0].textContent,'優勝戦');
  assert.equal(tiles[0].children[0].textContent,'開催');
  assert.equal(tiles[4].children[0].textContent,'開催なし');
  assert.match(tiles[23].getAttribute('aria-label'),/大村.*優勝戦/);
  console.log('PASS: Venue tiles use confirmed day labels, including the final-day label, with honest fallbacks for missing metadata.');

  await view.run('loadRaceStatus()');
  assert.equal(view.elements.get('raceButtons').children.length,12);
  for(let race=1;race<=12;race++){
    assert.equal(view.race(race).classes.has('settled'),[1,3,10].includes(race));
    assert.equal(view.race(race).disabled,false);
  }
  assert.equal(view.race(1).classes.has('active'),true);
  assert.match(view.race(1).getAttribute('aria-label'),/結果確定/);
  assert.match(view.race(2).getAttribute('aria-label'),/結果未確定/);
  assert.match(view.race(3).getAttribute('aria-label'),/中止・不成立/);
  view.race(10).onclick();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(view.elements.get('race').value,'10R');
  assert.equal(view.race(10).classes.has('active'),true);
  assert.equal(view.race(10).classes.has('settled'),true);
  assert.ok(view.calls.some(call=>call.url.includes('/api/saved_prediction?')&&call.url.includes('race=10')));
  assert.ok(view.calls.every(call=>!call.options?.method));
  assert.equal(view.statusCalls().length,1);
  console.log('PASS: Only confirmed or cancelled races receive the grey-state class; all 12 races remain selectable through read-only requests.');

  await view.run('loadRaceStatus()');assert.equal(view.statusCalls().length,1);
  view.advance(60001);await view.run('loadRaceStatus()');assert.equal(view.statusCalls().length,2);
  console.log('PASS: Race selection reuses its venue snapshot and live refresh is limited to once per minute.');

  let finishOld;
  const oldResponse=new Promise(resolve=>finishOld=resolve);
  const late=context(url=>url.includes('stadium=04')?oldResponse:response(result('20261017','24',[12],[])));
  const first=late.run('loadRaceStatus()');const joined=late.run('loadRaceStatus()');
  assert.equal(late.statusCalls().length,1);
  late.elements.get('stadium').value='24';late.run('renderRaceButtons()');
  assert.equal(late.race(1).classes.has('settled'),false);
  await late.run('loadRaceStatus()');
  finishOld(response(result()));await Promise.all([first,joined]);
  assert.equal(late.calls[0].options.signal.aborted,true);
  assert.equal(late.race(12).classes.has('settled'),true);
  assert.equal(late.race(1).classes.has('settled'),false);
  console.log('PASS: Concurrent callers share one request and late results for another venue cannot repaint the selected venue.');

  let oldDateResolve;
  const dateResponse=new Promise(resolve=>oldDateResolve=resolve);
  const dates=context(url=>url.includes('date=20261017')?dateResponse:response(result('20261016','04',[2],[])));
  const previous=dates.run('loadRaceStatus()');dates.elements.get('date').value='20261016';
  await dates.run('loadRaceStatus()');oldDateResolve(response(result()));await previous;
  assert.equal(dates.race(2).classes.has('settled'),true);
  assert.equal(dates.race(1).classes.has('settled'),false);
  console.log('PASS: Day changes keep the previous day results out of the newly selected date.');

  let offline=false;
  const failed=context(()=>offline?response({ok:false},503):undefined);
  await failed.run('loadRaceStatus()');failed.advance(60001);offline=true;
  await failed.run('loadRaceStatus()');failed.run('renderRaceButtons()');
  assert.equal(failed.race(1).classes.has('settled'),true);
  assert.match(failed.elements.get('raceStatusNote').textContent,/前回確認時点/);
  failed.elements.get('stadium').value='24';failed.run('renderRaceButtons()');
  await failed.run('loadRaceStatus()');
  assert.equal(failed.race(1).classes.has('settled'),false);
  assert.match(failed.race(1).getAttribute('aria-label'),/確認中/);
  assert.match(failed.elements.get('raceStatusNote').textContent,/取得できません/);
  console.log('PASS: A failed refresh preserves confirmed same-scope colours while another scope never inherits them.');

  const mismatch=context(()=>response(result('20261017','24',[1],[])));
  await mismatch.run('loadRaceStatus()');mismatch.run('renderRaceButtons()');
  assert.equal(mismatch.race(1).classes.has('settled'),false);
  assert.match(mismatch.elements.get('raceStatusNote').textContent,/取得できません/);
  const empty=context(()=>response(result('20261017','04',[],[])));
  await empty.run('loadRaceStatus()');
  assert.equal(empty.race(1).classes.has('settled'),false);
  assert.match(empty.race(1).getAttribute('aria-label'),/結果未確定/);
  assert.match(empty.elements.get('raceStatusNote').textContent,/白：結果未確定/);
  console.log('PASS: A valid day with no published results differs from a failed or wrongly scoped response.');

  let retries=0;
  const timeout=context(()=>++retries===1?new Promise(()=>{}):response(result()));
  const waiting=timeout.run('loadRaceStatus()');await new Promise(resolve=>setImmediate(resolve));
  [...timeout.timers.values()].find(timer=>timer.delay===20000).callback();await waiting;
  assert.equal(timeout.statusCalls()[0].options.signal.aborted,true);
  assert.equal(timeout.timers.size,0);
  await timeout.run('loadRaceStatus()');
  assert.equal(timeout.race(1).classes.has('settled'),true);
  console.log('PASS: A stalled status request times out, clears its timer, and permits a later successful retry.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
