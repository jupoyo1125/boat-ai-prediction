const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const definitions = script.slice(0, script.lastIndexOf("document.getElementById('date').addEventListener"));
const bindings = script.slice(script.lastIndexOf("document.getElementById('date').addEventListener"), script.lastIndexOf('setupDates();'));
const response = (data, status=200) => ({ok:status<400, status, json:async()=>data});
function stat(roi, wins=1, settled=2, pending=3) {
  const investment = settled*100;
  const payout = investment*roi/100;
  return {roi:settled ? roi : null, hit_rate:settled ? wins/settled*100 : null,
    settled, wins, pending, investment, payout, profit:payout-investment};
}
function payload(date) {
  return {ok:true, legacy_records:0,
    category_stats:{gachi:stat(110,4,10), roman:stat(120), oni:stat(90)},
    automatic_category_stats:{gachi:stat(80,3,10), roman:stat(120), oni:stat(90)},
    daily:{date, category_stats:{gachi:stat(250), roman:stat(50), oni:stat(300)},
      automatic_category_stats:{gachi:stat(150), roman:stat(0,0), oni:stat(200)}},
    historical:{ok:true, exists:true, phase:'running', enabled:true, total_days:365,
      category_stats:{gachi:stat(999), roman:stat(888), oni:stat(777)}},
  };
}
function context(handler, instant='2026-10-14T15:30:00Z') {
  class Clock extends Date {
    constructor(...args){super(...(args.length ? args : [instant]));}
    static now(){return new Date(instant).getTime();}
  }
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],
    {textContent:'-',innerHTML:'',value:'',disabled:false,style:{},listeners:{},
      addEventListener(name,callback){this.listeners[name]=callback;}}]));
  elements.get('perfScope').value='automatic';
  elements.get('date').value='20261007';
  const calls=[], timers=new Map(); let nextTimer=0;
  const ctx=vm.createContext({Date:Clock,URLSearchParams,AbortController,console:{error(){}},
    document:{getElementById:id=>elements.get(id)},
    setTimeout(callback,delay){const id=++nextTimer;timers.set(id,{callback,delay});return id;},
    clearTimeout(id){timers.delete(id);},
    async fetch(url,options){
      calls.push({url,options});
      if(handler){const custom=await handler(url,options);if(custom)return custom;}
      if(url.startsWith('/api/performance?')){
        const date=new URLSearchParams(url.split('?')[1]).get('date');
        return response(payload(date));
      }
      if(url==='/api/historical')return response(payload('20261015').historical);
      throw new Error('Unexpected request '+url);
    }});
  vm.runInContext(definitions,ctx);
  vm.runInContext(bindings,ctx);
  return {elements,calls,run:code=>vm.runInContext(code,ctx),expire(delay){
    for(const [id,timer] of timers)if(timer.delay===delay){timers.delete(id);timer.callback();}
  }};
}

async function run(){
  const day=context();await day.run('loadPerformanceStatus()');
  assert.equal(day.elements.get('perfDate').value,'2026-10-15');
  assert.equal(day.calls[0].url,'/api/performance?date=20261015');
  assert.equal(day.elements.get('perfDailyGachiRoi').textContent,'150.0%');
  assert.equal(day.elements.get('perfGachiRoi').textContent,'80.0%');
  assert.equal(day.elements.get('historyPerfGachiRoi').textContent,'999.0%');
  assert.equal(day.elements.get('perfDailyGachiProfit').textContent,'+100円');
  assert.equal(day.elements.get('perfGachiProfit').textContent,'-200円');
  assert.match(day.elements.get('perfDailyGachiSummary').textContent,/的中1R｜結果待ち3R/);
  for(const category of ['Gachi','Roman','Oni'])assert.notEqual(day.elements.get('perfDaily'+category+'Roi').textContent,'-');
  console.log('PASS: Japan today shows three daily categories, separate cumulative totals and separate replay results.');

  day.elements.get('perfDate').value='2026-10-10';
  day.run('performanceFollowingToday=false');await day.run('loadPerformanceStatus()');
  assert.match(day.calls.at(-1).url,/date=20261010/);
  assert.equal(day.elements.get('perfGachiRoi').textContent,'80.0%');
  assert.match(day.elements.get('perfDailyNote').textContent,/2026\/10\/10/);
  await day.run('showTodayPerformance()');
  assert.equal(day.elements.get('perfDate').value,'2026-10-15');
  assert.equal(day.elements.get('date').value,'20261007');
  console.log('PASS: A past day and the Today button change only the report date, leaving the cumulative period and race selection intact.');

  day.elements.get('perfScope').value='all';await day.run('loadPerformanceStatus()');
  assert.equal(day.elements.get('perfDailyGachiRoi').textContent,'250.0%');
  assert.equal(day.elements.get('perfGachiRoi').textContent,'110.0%');
  assert.match(day.elements.get('perfDailyNote').textContent,/手動予想を含む/);
  console.log('PASS: Changing forecast type applies the same scope to daily and cumulative performance.');

  const pending=context(url=>{
    const data=payload('20261015');
    data.daily.automatic_category_stats={gachi:stat(0,0,0,2),roman:stat(0,0,0,0),oni:stat(0,0,0,1)};
    return response(data);
  });await pending.run('loadPerformanceStatus()');
  assert.equal(pending.elements.get('perfDailyGachiRoi').textContent,'-');
  assert.equal(pending.elements.get('perfDailyGachiHitRate').textContent,'-');
  assert.equal(pending.elements.get('perfDailyGachiProfit').textContent,'-');
  assert.equal(pending.elements.get('perfDailyGachiSettled').textContent,'0R');
  assert.match(pending.elements.get('perfDailyGachiSummary').textContent,/結果待ち2R/);
  assert.match(pending.elements.get('perfDailyRomanSummary').textContent,/保存記録がありません/);
  console.log('PASS: Pending results and an empty day remain distinct and do not appear as zero-percent losses.');

  let finishOld;
  const old=new Promise(resolve=>{finishOld=resolve;});
  const raced=context(url=>url.endsWith('20261014')?old:undefined);
  raced.elements.get('perfDate').value='2026-10-14';raced.run('performanceFollowingToday=false');
  const first=raced.run('loadPerformanceStatus()');
  const joined=raced.run('loadPerformanceStatus()');
  assert.equal(raced.calls.length,1);
  raced.elements.get('perfDate').value='2026-10-15';await raced.run('loadPerformanceStatus()');
  const stale=payload('20261014');stale.daily.automatic_category_stats.gachi=stat(500);
  finishOld(response(stale));await Promise.all([first,joined]);
  assert.equal(raced.elements.get('perfDailyGachiRoi').textContent,'150.0%');
  assert.match(raced.elements.get('perfDailyNote').textContent,/2026\/10\/15/);
  assert.equal(raced.calls[0].options.signal.aborted,true);
  console.log('PASS: Concurrent refreshes share a request and a late response for an old day cannot overwrite the selected day.');

  const failed=context(url=>url.endsWith('20261010')?new Promise(()=>{}):undefined);
  await failed.run('loadPerformanceStatus()');
  failed.elements.get('perfDate').value='2026-10-10';failed.run('performanceFollowingToday=false');
  const waiting=failed.run('loadPerformanceStatus()');
  assert.equal(failed.elements.get('perfDailyGachiRoi').textContent,'-');
  failed.expire(20000);await waiting;
  assert.match(failed.elements.get('perfNote').textContent,/取得できません/);
  assert.match(failed.elements.get('perfDailyNote').textContent,/2026\/10\/10/);
  assert.equal(failed.elements.get('perfGachiRoi').textContent,'80.0%');
  console.log('PASS: A stalled daily request stops after its deadline without displaying another day under the new date.');

  const before=day.elements.get('perfDailyGachiRoi').textContent;
  await day.run('loadHistoricalStatus()');
  assert.equal(day.elements.get('perfDailyGachiRoi').textContent,before);
  assert.equal(day.elements.get('perfGachiRoi').textContent,'110.0%');
  console.log('PASS: Historical progress refreshes never replace daily or cumulative live performance.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
