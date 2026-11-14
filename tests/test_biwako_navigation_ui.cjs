const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const source=html.split('<script>')[1].split('</script>')[0];
new vm.Script(source);
const definitions=source.slice(0,source.lastIndexOf("document.getElementById('date').addEventListener"));
function element(){
 const attrs={},classes=new Set();
 let markup='';
 return {
  children:[],value:'',textContent:'',hidden:false,disabled:false,
  classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x)},
  setAttribute:(name,val)=>{attrs[name]=String(val)},
  getAttribute:name=>attrs[name],
  removeAttribute:name=>{delete attrs[name]},
  scrollIntoView(){},
  appendChild(child){this.children.push(child)},
  get innerHTML(){return markup;},
  set innerHTML(value){markup=value;this.children=[];},
  classes
 };
}
const elements=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],element()]));
elements.get('date').value='20261114';
elements.get('stadium').value='04';
elements.get('race').value='1R';
elements.get('fixed').value='none';
const pages=[elements.get('pageRaces'),elements.get('pagePerformance'),elements.get('pageLearning')];
pages[0].getAttribute=()=> 'races';pages[1].getAttribute=()=> 'performance';pages[2].getAttribute=()=> 'learning';
const nav=[elements.get('navRaces'),elements.get('navBiwako'),elements.get('navPerformance'),elements.get('navLearning')];
nav.forEach((link,i)=>{
 const originalGet=link.getAttribute;
 link.getAttribute=name=>name==='data-nav'?['races','biwako','performance','learning'][i]:originalGet(name);
});
let selected=[],updated=0;
const ctx=vm.createContext({
 Date,URLSearchParams,AbortController,setTimeout,clearTimeout,console,
 window:{location:{hash:'#biwako'},scrollTo(){}},
 document:{getElementById:id=>elements.get(id),createElement:element,
  querySelectorAll:selector=>selector==='[data-page]'?pages:selector==='[data-nav]'?nav:[]},
 fetch:async url=>{throw Error('Unexpected background request '+url)}
});
vm.runInContext(definitions,ctx);
vm.runInContext('selectVenue=(stadium,target)=>recordSelection(stadium,target);loadSelectedSavedPrediction=()=>recordReload()',ctx);
ctx.recordSelection=(stadium,target)=>selected.push({stadium,target});
ctx.recordReload=()=>updated++;
assert.match(html,/href="#biwako".*びわこ展開分析/);
assert.match(html,/id="biwakoRaceButtons"/);
ctx.evalCode=code=>vm.runInContext(code,ctx);

ctx.evalCode('showPage("biwako")');
assert.equal(ctx.evalCode('activePage'),'races');
assert.equal(elements.get('pageRaces').hidden,false);
assert.equal(elements.get('navBiwako').getAttribute('aria-current'),'page');
assert.equal(elements.get('navRaces').getAttribute('aria-current'),undefined);

ctx.evalCode('requestBiwakoFocus()');
assert.equal(ctx.evalCode('biwakoShortcutPending'),true,'opening before schedule should be deferred');
assert.deepEqual(selected,[]);
ctx.evalCode('scheduleLoadedDate="20261114";listedVenues=[{stadium:"11",venue:"びわこ"}];');
ctx.evalCode('focusBiwakoWhenReady()');
assert.deepEqual(selected,[{stadium:'11',target:'biwakoTacticsPanel'}]);
assert.equal(ctx.evalCode('biwakoShortcutPending'),false);
console.log('PASS: direct Biwako tab opens the correct page and defers venue selection until schedule is ready.');

elements.get('stadium').value='11';
ctx.evalCode('renderBiwakoRaceButtons()');
let buttons=elements.get('biwakoRaceButtons').children;
assert.equal(buttons.length,12);
assert.equal(buttons[0].getAttribute('aria-pressed'),'true');
assert.equal(buttons[0].classList.contains('active'),true);
buttons[6].onclick();
assert.equal(elements.get('race').value,'7R');
assert.equal(updated,1);
ctx.evalCode('renderBiwakoRaceButtons()');
buttons=elements.get('biwakoRaceButtons').children;
assert.equal(buttons[6].classList.contains('active'),true);
assert.equal(elements.get('biwakoSelectedRace').textContent,'びわこ 7R');
console.log('PASS: 12 race shortcut buttons follow the selected race and request its analysis.');

ctx.evalCode('listedVenues=[];requestBiwakoFocus()');
assert.match(elements.get('scheduleNotice').textContent,/開催がありません/);
assert.deepEqual(selected,[{stadium:'11',target:'biwakoTacticsPanel'}]);
console.log('PASS: unavailable race dates do not fabricate an active Biwako meeting.');
