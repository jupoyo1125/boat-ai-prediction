const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const source=html.split('<script>')[1].split('</script>')[0];
new vm.Script(source);
const definitions=source.slice(0,source.lastIndexOf("document.getElementById('date').addEventListener"));
function element(tag='div'){
 const attrs={}; const classes=new Set(); let value='',markup='';
 const el={tag,children:[],hidden:false,disabled:false,textContent:'',style:{},
   getAttribute:k=>attrs[k]??null,setAttribute:(k,v)=>attrs[k]=String(v),removeAttribute:k=>delete attrs[k],
   classList:{add:c=>classes.add(c),remove:c=>classes.delete(c)},
   appendChild(child){this.children.push(child);if(this.tag==='select'&&this.children.length===1)value=child.value;},
   addEventListener(){}};
 Object.defineProperty(el,'value',{get:()=>value,set:v=>value=v});
 Object.defineProperty(el,'innerHTML',{get:()=>markup,set:v=>{markup=v;el.children=[];if(tag==='select')value='';}});
 Object.defineProperty(el,'options',{get:()=>el.children});
 Object.defineProperty(el,'selectedIndex',{get:()=>el.children.findIndex(n=>n.value===value)});
 return el;
}
const nodes=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],element(['date','stadium','race','fixed'].includes(m[1])?'select':'div')]));
for(const [id,name]of [['pageRaces','races'],['pagePerformance','performance'],['pageLearning','learning']])nodes.get(id).setAttribute('data-page',name);
for(const [id,name]of [['navRaces','races'],['navPerformance','performance'],['navLearning','learning']])nodes.get(id).setAttribute('data-nav',name);
const cards=['gachi','roman','oni'].map(name=>{const n=element();n.setAttribute('data-bet-card',name);return n;});
const categories=['gachi','roman','oni'].map(name=>{const n=element();n.setAttribute('data-category',name);return n;});
for(const [date,text]of [['20261017','2026/10/17'],['20261016','2026/10/16']]){const n=element('option');n.value=date;n.textContent=text;nodes.get('date').appendChild(n);}
nodes.get('race').value='5R';nodes.get('fixed').value='none';
const calls=[],timers=new Map();let next=0;
const ctx=vm.createContext({Date,URLSearchParams,AbortController,console,
 window:{location:{hash:'#races'},scrollTo(){}},
 setTimeout(fn){const id=++next;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id),
 document:{getElementById:id=>nodes.get(id),createElement:element,querySelectorAll(selector){
   if(selector==='[data-page]')return ['pageRaces','pagePerformance','pageLearning'].map(id=>nodes.get(id));
   if(selector==='[data-nav]')return ['navRaces','navPerformance','navLearning'].map(id=>nodes.get(id));
   if(selector==='[data-bet-card]')return cards;if(selector==='[data-category]')return categories;
   if(selector==='.venue-tile')return nodes.get('raceList').children;
   if(selector==='.race-btn')return nodes.get('raceButtons').children;
   return [];
 }},
 async fetch(url,options){calls.push({url,options});
  if(url.startsWith('/api/schedule?'))return {ok:true,json:async()=>({ok:true,date:new URLSearchParams(url.split('?')[1]).get('date'),venues:[{stadium:'04',venue:'平和島'},{stadium:'01',venue:'桐生'}]})};
  if(url.startsWith('/api/race_status?')){const params=new URLSearchParams(url.split('?')[1]);return {ok:true,json:async()=>({ok:true,date:params.get('date'),stadium:params.get('stadium'),settled_races:[],cancelled_races:[]})};}
  if(url.startsWith('/api/saved_prediction?'))return {ok:false,json:async()=>({ok:false})};
  throw Error('Unexpected request '+url);
 }});
vm.runInContext(definitions,ctx);const run=code=>vm.runInContext(code,ctx);
async function main(){
 run("showPage('races')");assert.equal(nodes.get('pageRaces').hidden,false);assert.equal(nodes.get('pagePerformance').hidden,true);
 run("showPage('performance')");assert.equal(nodes.get('pageRaces').hidden,true);assert.equal(nodes.get('pagePerformance').hidden,false);
 assert.equal(nodes.get('navPerformance').getAttribute('aria-current'),'page');assert.equal(nodes.get('navRaces').getAttribute('aria-current'),null);
 run("showPage('learning')");assert.equal(nodes.get('pageLearning').hidden,false);
 assert.equal(nodes.get('date').value,'20261017');assert.equal(nodes.get('race').value,'5R');assert.equal(calls.length,0);
 console.log('PASS: View navigation separates races, performance and learning without changing race selection or submitting requests.');
 await run('loadSchedule()');
 const tiles=nodes.get('raceList').children;assert.equal(tiles.length,24);assert.equal(tiles.filter(b=>!b.disabled).length,2);
 assert.equal(tiles[3].disabled,false);assert.equal(tiles[4].disabled,true);assert.equal(tiles[3].getAttribute('aria-pressed'),'true');
 assert.equal(nodes.get('selectedRaceTitle').textContent,'平和島 5Rの予想');assert.equal(nodes.get('raceButtons').children.length,12);
 console.log('PASS: The 24-venue directory uses actual schedule availability and highlights the selected venue.');
 tiles[0].onclick();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(nodes.get('stadium').value,'01');assert.equal(nodes.get('race').value,'5R');assert.equal(nodes.get('selectedRaceTitle').textContent,'桐生 5Rの予想');
 const race12=nodes.get('raceButtons').children[11];race12.onclick();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(nodes.get('race').value,'12R');assert.equal(nodes.get('raceButtons').children[11].getAttribute('aria-pressed'),'true');
 assert.ok(calls.some(c=>c.url.includes('race=12')));assert.ok(calls.every(c=>!c.options?.method));
 console.log('PASS: Venue and 12R buttons load saved predictions through read-only requests.');
 run("showBetCategory('roman')");assert.equal(cards[0].hidden,true);assert.equal(cards[1].hidden,false);assert.equal(cards[2].hidden,true);
 assert.equal(categories[1].getAttribute('aria-pressed'),'true');assert.equal(categories[0].getAttribute('aria-pressed'),'false');
 console.log('PASS: All three bet categories remain available in separate, accessible views.');
 run('shiftRaceDate(-1)');await new Promise(resolve=>setImmediate(resolve));
 assert.equal(nodes.get('date').value,'20261016');assert.equal(nodes.get('previousRaceDay').disabled,true);assert.equal(nodes.get('nextRaceDay').disabled,false);
 run('shiftRaceDate(1)');await new Promise(resolve=>setImmediate(resolve));assert.equal(nodes.get('date').value,'20261017');assert.equal(nodes.get('nextRaceDay').disabled,true);
 console.log('PASS: Previous and next date controls stay within available dates and refresh the selected day.');
 assert.equal(new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1])).size,[...html.matchAll(/\bid="([^"]+)"/g)].length);
 const betTables=[...html.matchAll(/<table class="bet-table">([\s\S]*?)<\/table>/g)];
 assert.equal(betTables.length,3);
 for(const table of betTables){
  assert.deepEqual([...table[1].matchAll(/<th scope="col">([^<]+)<\/th>/g)].map(match=>match[1]),['買い目','オッズ']);
  assert.doesNotMatch(table[1],/順位|推定確率|期待倍率|推奨理由/);
 }
 assert.doesNotMatch(html,/grid-template-columns:26px repeat\(3,minmax\(0,1fr\)\)/);
 console.log('PASS: Every data binding has a unique ID and all three compact bet tables expose only combinations and odds.');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
