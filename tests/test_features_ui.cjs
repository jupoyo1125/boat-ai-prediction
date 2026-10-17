const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const definitions = script.slice(0, script.lastIndexOf("document.getElementById('date').addEventListener"));
const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1],
  {textContent:'', innerHTML:'', disabled:false, style:{}}]));
let weights = {motor:.15,exhibition:.30,meeting:.05,course:.50};
const ctx = vm.createContext({console, Date, URLSearchParams, AbortController, setTimeout, clearTimeout,
  document:{getElementById:id=>elements.get(id)},
  fetch:async()=>({ok:true,json:async()=>({ok:true,model:{feature_schema:'racer-course-year-v2',
    weights, feature_samples:0,feature_hits:0,
    samples:1730,hits:707,temperature:12}})})});
vm.runInContext(definitions, ctx);
async function run(){
  await vm.runInContext('loadLearningStatus()',ctx);
  assert.equal(elements.get('learnSamples').textContent,'0回');
  assert.equal(elements.get('learnHits').textContent,'0回');
  assert.match(elements.get('learnWeights').innerHTML,/モーター2連対率/);
  assert.match(elements.get('learnWeights').innerHTML,/節間成績/);
  assert.match(elements.get('learnWeights').innerHTML,/1・2・3着率/);
  assert.doesNotMatch(elements.get('learnWeights').innerHTML,/全国勝率|当地勝率|過去実績/);
  assert.match(elements.get('learnBasis').textContent,/1,730R/);
  for (const percent of ['15.0%', '30.0%', '5.0%', '50.0%']) {
    assert.ok(elements.get('learnWeights').innerHTML.includes('>'+percent+'<'));
  }
  assert.match(elements.get('learnBasis').textContent,/モーター15%・展示タイム30%・節間成績5%・選手別コース成績50%/);
  assert.doesNotMatch(elements.get('learnBasis').textContent,/各25%/);
  console.log('PASS: Four-feature weights and new learning counts stay separate from legacy lifetime counts.');
  weights = {motor:.16,exhibition:.29,meeting:.05,course:.50};
  await vm.runInContext('loadLearningStatus()',ctx);
  assert.match(elements.get('learnWeights').innerHTML,/>16.0%</);
  assert.match(elements.get('learnWeights').innerHTML,/>29.0%</);
  console.log('PASS: Requested baseline is explained while the percentages display the actual learned model weights.');
  const detail = vm.runInContext(`featureDetails({feature_schema:'racer-course-year-v2',motor:40.5,exhibition:6.7,
    meeting_count:2,meeting_finishes:[{place:1},{place:3}],entry_course:4,
    course_rates:{starts:20,first:0,second:25,third:15},course_start_date:'20251017',course_end_date:'20261016'})`,ctx);
  assert.match(detail,/1着 0.0% \/ 2着 25.0% \/ 3着 15.0%/);
  assert.match(detail,/4コース \/ 過去1年 20走/);
  assert.match(detail,/2025\/10\/17～2026\/10\/16/);
  const missing = vm.runInContext("featureDetails({feature_schema:'racer-course-year-v2',course_rates:{starts:0}})",ctx);
  assert.match(missing,/展示進入 未確定/);
  assert.match(missing,/1着 成績なし/);
  assert.doesNotMatch(missing,/0.0%/);
  console.log('PASS: Raw finish rates, starts and dates display correctly; no history is distinct from a zero rate.');
  vm.runInContext("renderHistorical({exists:true,phase:'completed',samples:1000,feature_compatible:false})",ctx);
  assert.equal(elements.get('historyApply').disabled,true);
  assert.match(elements.get('historyStatus').textContent,/旧特徴/);
  console.log('PASS: The old historical model is labeled and cannot be applied to the four-feature model.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
