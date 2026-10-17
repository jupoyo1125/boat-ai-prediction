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
function payload(date, stadium='') {
  const hit=(race,payout,actual_combo='1-2-3')=>({date,stadium:stadium||'04',
    venue:stadium==='06'?'浜名湖':stadium==='24'?'大村':'平和島',race,actual_combo,payout});
  return {ok:true, legacy_records:0,
    category_stats:{gachi:stat(110,4,10), roman:stat(120), oni:stat(90)},
    automatic_category_stats:{gachi:stat(80,3,10), roman:stat(120), oni:stat(90)},
    daily:{date, stadium, category_stats:{gachi:stat(250), roman:stat(50), oni:stat(300)},
      automatic_category_stats:{gachi:stat(150), roman:stat(0,0), oni:stat(200)},
      category_hits:{gachi:[hit(1,500)],roman:[hit(2,100,'4-5-6')],oni:[hit(3,600)]},
      automatic_category_hits:{gachi:[hit(1,300)],roman:[],oni:[hit(3,400)]}},
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
    {textContent:'-',innerHTML:'',value:'',disabled:false,style:{},listeners:{},open:false,
      showModal(){this.open=true;},
      close(){this.open=false;this.listeners.close?.();},
      addEventListener(name,callback){this.listeners[name]=callback;}}]));
  elements.get('perfScope').value='automatic';
  elements.get('date').value='20261007';
  elements.get('stadium').value='06';
  const venueOptions = new Map([...html.match(/<select id="perfStadium">([\s\S]*?)<\/select>/)[1]
    .matchAll(/<option value="([^"]*)">([^<]+)<\/option>/g)].map(match=>[match[1],match[2]]));
  Object.defineProperty(elements.get('perfStadium'),'selectedOptions',{
    get(){return [{textContent:venueOptions.get(this.value)}];},
  });
  const calls=[], timers=new Map(); let nextTimer=0;
  const ctx=vm.createContext({Date:Clock,URLSearchParams,AbortController,console:{error(){}},
    document:{getElementById:id=>elements.get(id)},
    setTimeout(callback,delay){const id=++nextTimer;timers.set(id,{callback,delay});return id;},
    clearTimeout(id){timers.delete(id);},
    async fetch(url,options){
      calls.push({url,options});
      if(handler){const custom=await handler(url,options);if(custom)return custom;}
      if(url.startsWith('/api/performance?')){
        const params=new URLSearchParams(url.split('?')[1]);
        return response(payload(params.get('date'),params.get('stadium')||''));
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

  const venueDay=context(url=>{
    const params=new URLSearchParams(url.split('?')[1]);
    const data=payload(params.get('date'),params.get('stadium')||'');
    if(params.get('stadium')==='04'){
      data.daily.automatic_category_stats.gachi=stat(300);
      data.daily.category_stats.gachi=stat(400);
    }
    return response(data);
  });
  await venueDay.run('loadPerformanceStatus()');
  venueDay.elements.get('perfStadium').value='04';
  await venueDay.elements.get('perfStadium').listeners.change();
  assert.equal(venueDay.calls.at(-1).url,'/api/performance?date=20261015&stadium=04');
  assert.equal(venueDay.elements.get('perfDailyGachiRoi').textContent,'300.0%');
  assert.equal(venueDay.elements.get('perfGachiRoi').textContent,'80.0%');
  assert.match(venueDay.elements.get('perfDailyNote').textContent,/2026\/10\/15｜平和島/);
  assert.equal(venueDay.elements.get('stadium').value,'06');
  venueDay.elements.get('perfScope').value='all';await venueDay.run('loadPerformanceStatus()');
  assert.equal(venueDay.elements.get('perfDailyGachiRoi').textContent,'400.0%');
  assert.equal(venueDay.elements.get('perfGachiRoi').textContent,'110.0%');
  venueDay.elements.get('perfDate').value='2026-10-10';
  venueDay.run('performanceFollowingToday=false');await venueDay.run('loadPerformanceStatus()');
  assert.equal(venueDay.elements.get('perfStadium').value,'04');
  await venueDay.run('showTodayPerformance()');
  assert.equal(venueDay.calls.at(-1).url,'/api/performance?date=20261015&stadium=04');
  venueDay.elements.get('perfStadium').value='';await venueDay.elements.get('perfStadium').listeners.change();
  assert.equal(venueDay.elements.get('perfDailyGachiRoi').textContent,'250.0%');
  assert.match(venueDay.elements.get('perfDailyNote').textContent,/全場/);
  console.log('PASS: Venue selection filters the daily report, survives date/type changes and Today, and leaves cumulative totals and prediction selection independent.');

  let finishVenue;
  const oldVenue=new Promise(resolve=>{finishVenue=resolve;});
  const venueRace=context(url=>new URLSearchParams(url.split('?')[1]).get('stadium')==='04'?oldVenue:undefined);
  venueRace.elements.get('perfStadium').value='04';
  const previousVenue=venueRace.run('loadPerformanceStatus()');
  const sameVenue=venueRace.run('loadPerformanceStatus()');
  assert.equal(venueRace.calls.length,1);
  venueRace.elements.get('perfStadium').value='06';await venueRace.run('loadPerformanceStatus()');
  const staleVenue=payload('20261015','04');staleVenue.daily.automatic_category_stats.gachi=stat(500);
  finishVenue(response(staleVenue));await Promise.all([previousVenue,sameVenue]);
  assert.equal(venueRace.elements.get('perfDailyGachiRoi').textContent,'150.0%');
  assert.match(venueRace.elements.get('perfDailyNote').textContent,/浜名湖/);
  assert.equal(venueRace.calls[0].options.signal.aborted,true);
  console.log('PASS: Requests for the same date/type/venue are shared and a late response for another venue cannot replace the current venue.');

  const unavailableVenue=context(url=>new URLSearchParams(url.split('?')[1]).get('stadium')==='04'?new Promise(()=>{}):undefined);
  await unavailableVenue.run('loadPerformanceStatus()');
  unavailableVenue.elements.get('perfStadium').value='04';
  const venueWait=unavailableVenue.run('loadPerformanceStatus()');
  assert.equal(unavailableVenue.elements.get('perfDailyGachiRoi').textContent,'-');
  unavailableVenue.expire(20000);await venueWait;
  assert.match(unavailableVenue.elements.get('perfDailyNote').textContent,/平和島.*取得できません/);
  assert.equal(unavailableVenue.elements.get('perfGachiRoi').textContent,'80.0%');
  assert.doesNotMatch(unavailableVenue.elements.get('perfNote').textContent,/前回確認時点/);
  console.log('PASS: A venue request timeout clears the previous venue daily data and preserves the all-venue cumulative report.');

  const wrongVenue=context(()=>response(payload('20261015','06')));
  wrongVenue.elements.get('perfStadium').value='04';await wrongVenue.run('loadPerformanceStatus()');
  assert.equal(wrongVenue.elements.get('perfDailyGachiRoi').textContent,'-');
  assert.match(wrongVenue.elements.get('perfNote').textContent,/日次実績を取得できません/);
  console.log('PASS: Data marked for another venue is never shown under the selected venue.');

  const emptyVenue=context(url=>{
    const params=new URLSearchParams(url.split('?')[1]);
    const data=payload(params.get('date'),params.get('stadium')||'');
    data.daily.automatic_category_stats={gachi:stat(0,0,0,0),roman:stat(0,0,0,0),oni:stat(0,0,0,0)};
    return response(data);
  });
  emptyVenue.elements.get('perfStadium').value='24';await emptyVenue.run('loadPerformanceStatus()');
  for(const category of ['Gachi','Roman','Oni']){
    assert.equal(emptyVenue.elements.get('perfDaily'+category+'Roi').textContent,'-');
    assert.equal(emptyVenue.elements.get('perfDaily'+category+'Settled').textContent,'0R');
    assert.match(emptyVenue.elements.get('perfDaily'+category+'Summary').textContent,/保存記録がありません/);
  }
  assert.match(emptyVenue.elements.get('perfDailyNote').textContent,/大村/);
  console.log('PASS: A venue with no stored forecasts shows no-record status without inventing losses.');

  const hits=context();await hits.run('loadPerformanceStatus()');
  const dialog=hits.elements.get('perfHitsDialog');
  const callsBeforeClick=hits.calls.length;
  assert.equal(hits.elements.get('perfDailyGachiHitRate').disabled,false);
  hits.elements.get('perfDailyGachiHitRate').listeners.click();
  assert.equal(dialog.open,true);
  assert.match(hits.elements.get('perfHitsTitle').textContent,/ガチガチ/);
  assert.match(hits.elements.get('perfHitsContext').textContent,/2026\/10\/15｜全場｜自動予想/);
  assert.match(hits.elements.get('perfHitsRows').innerHTML,/<td>平和島<\/td><td>1R<\/td><td>1-2-3<\/td><td>300円<\/td>/);
  assert.equal(hits.elements.get('perfHitsTotal').textContent,'的中1R｜払戻合計 300円');
  assert.equal(hits.calls.length,callsBeforeClick);
  for(const id of ['Gachi','Roman','Oni']){
    assert.match(html,new RegExp('<button[^>]*id="perfDaily'+id+'HitRate"[^>]*aria-haspopup="dialog"'));
    assert.match(html,new RegExp('<b id="perf'+id+'HitRate">'));
    assert.equal(hits.elements.get('perf'+id+'HitRate').listeners.click,undefined);
  }
  hits.elements.get('perfHitsClose').listeners.click();
  assert.equal(dialog.open,false);
  assert.equal(hits.run('performanceHitsCategory'),'');
  console.log('PASS: Only daily hit rates open a read-only venue/race/result/100-yen payout list; cumulative totals stay plain text.');

  hits.elements.get('perfDailyRomanHitRate').listeners.click();
  assert.equal(dialog.open,true);
  assert.equal(hits.elements.get('perfHitsRows').innerHTML,'');
  assert.equal(hits.elements.get('perfHitsEmpty').hidden,false);
  assert.equal(hits.elements.get('perfHitsTotal').textContent,'的中0R｜払戻合計 0円');
  dialog.close();
  hits.elements.get('perfDailyOniHitRate').listeners.click();
  assert.match(hits.elements.get('perfHitsTitle').textContent,/鬼しぼり/);
  assert.match(hits.elements.get('perfHitsRows').innerHTML,/<td>3R<\/td><td>1-2-3<\/td><td>400円/);
  assert.equal(pending.elements.get('perfDailyGachiHitRate').disabled,true);
  pending.run("openDailyHits('gachi')");
  assert.equal(pending.elements.get('perfHitsDialog').open,false);
  console.log('PASS: Each category uses its own winners and payout, zero hits show an empty list, and unconfirmed results cannot open invented details.');

  hits.elements.get('perfScope').value='all';
  const changingScope=hits.run('loadPerformanceStatus()');
  assert.equal(dialog.open,false);
  assert.equal(hits.elements.get('perfDailyOniHitRate').disabled,true);
  await changingScope;
  hits.elements.get('perfDailyRomanHitRate').listeners.click();
  assert.match(hits.elements.get('perfHitsContext').textContent,/手動予想を含む/);
  assert.match(hits.elements.get('perfHitsRows').innerHTML,/<td>2R<\/td><td>4-5-6<\/td><td>100円/);
  hits.elements.get('perfStadium').value='06';await hits.run('loadPerformanceStatus()');
  assert.equal(dialog.open,false);
  hits.elements.get('perfDailyGachiHitRate').listeners.click();
  assert.match(hits.elements.get('perfHitsContext').textContent,/浜名湖/);
  assert.match(hits.elements.get('perfHitsRows').innerHTML,/<td>浜名湖<\/td>/);
  assert.doesNotMatch(hits.elements.get('perfHitsRows').innerHTML,/平和島/);
  hits.elements.get('perfDate').value='2026-10-10';hits.run('performanceFollowingToday=false');
  await hits.run('loadPerformanceStatus()');
  assert.equal(dialog.open,false);
  hits.elements.get('perfDailyGachiHitRate').listeners.click();
  assert.match(hits.elements.get('perfHitsContext').textContent,/2026\/10\/10｜浜名湖/);
  console.log('PASS: Changing date, venue, or forecast scope closes old details and opens only the new filter results.');

  let refreshed=false;
  const refreshing=context(()=>{
    const data=payload('20261015');
    if(refreshed){
      data.daily.automatic_category_stats.gachi=stat(450);
      data.daily.automatic_category_hits.gachi[0].payout=900;
    }
    return response(data);
  });
  await refreshing.run('loadPerformanceStatus()');
  refreshing.elements.get('perfDailyGachiHitRate').listeners.click();
  refreshed=true;await refreshing.run('loadPerformanceStatus()');
  assert.equal(refreshing.elements.get('perfHitsDialog').open,true);
  assert.equal(refreshing.elements.get('perfDailyGachiHitRate').textContent,'50.0%');
  assert.match(refreshing.elements.get('perfHitsRows').innerHTML,/900円/);
  assert.equal(refreshing.elements.get('perfHitsTotal').textContent,'的中1R｜払戻合計 900円');
  console.log('PASS: Automatic refresh updates an open list without closing it or leaving stale payouts behind.');

  const missingDetails=context(()=>{
    const data=payload('20261015');delete data.daily.automatic_category_hits;
    return response(data);
  });await missingDetails.run('loadPerformanceStatus()');
  assert.equal(missingDetails.elements.get('perfDailyGachiHitRate').textContent,'50.0%');
  assert.equal(missingDetails.elements.get('perfDailyGachiHitRate').disabled,true);
  missingDetails.run("openDailyHits('gachi')");
  assert.equal(missingDetails.elements.get('perfHitsDialog').open,false);
  const escaped=context(()=>{
    const data=payload('20261015');data.daily.automatic_category_hits.gachi[0].venue='<img src=x onerror=alert(1)>';
    return response(data);
  });await escaped.run('loadPerformanceStatus()');
  escaped.elements.get('perfDailyGachiHitRate').listeners.click();
  assert.match(escaped.elements.get('perfHitsRows').innerHTML,/&lt;img/);
  assert.doesNotMatch(escaped.elements.get('perfHitsRows').innerHTML,/<img/);
  console.log('PASS: Missing details disable drilldown without inventing rows, and saved text is escaped before display.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
