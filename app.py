from flask import Flask, jsonify, request, send_from_directory
from itertools import permutations
from datetime import datetime, timedelta
from pathlib import Path
import json
import re, requests, math, time
from bs4 import BeautifulSoup
from odds_parser import parse_odds
from model import load as load_model, save as save_model, learn_from_record

app=Flask(__name__,static_folder='static')

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    return response

@app.route('/health', methods=['GET','OPTIONS'])
def health():
    return jsonify({'ok':True,'service':'boat-ai-api-v2','status':'live'})
    
BASE='https://www.boatrace.jp/owpc/pc/race/'
HEAD={'User-Agent':'Mozilla/5.0 (compatible; BOAT-AI/4.0)'}
LEDGER=Path('performance_ledger.json')

STADIUMS={'01':'桐生','02':'戸田','03':'江戸川','04':'平和島','05':'多摩川','06':'浜名湖','07':'蒲郡','08':'常滑','09':'津','10':'三国','11':'びわこ','12':'住之江','13':'尼崎','14':'鳴門','15':'丸亀','16':'児島','17':'宮島','18':'徳山','19':'下関','20':'若松','21':'芦屋','22':'福岡','23':'唐津','24':'大村'}



def load_ledger():
    if not LEDGER.exists(): return []
    try: return json.loads(LEDGER.read_text(encoding='utf-8'))
    except Exception: return []

def save_ledger(rows):
    LEDGER.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')

def ledger_stats(rows):
    bets=sum(float(r.get('investment',0) or 0) for r in rows)
    payouts=sum(float(r.get('payout',0) or 0) for r in rows)
    profit=payouts-bets
    wins=sum(1 for r in rows if r.get('hit'))
    settled=sum(1 for r in rows if r.get('settled',True))
    current=0; max_loss=0; streaks=[]
    for r in rows:
        if not r.get('settled',True): continue
        if r.get('hit'): current=0
        else: current+=1; max_loss=max(max_loss,current)
    roi=(payouts/bets*100) if bets else None
    hit_rate=(wins/settled*100) if settled else None
    by_month={}
    for r in rows:
        key=str(r.get('date',''))[:6] or 'unknown'; z=by_month.setdefault(key,{'investment':0,'payout':0,'profit':0,'races':0,'wins':0})
        inv=float(r.get('investment',0) or 0); pay=float(r.get('payout',0) or 0)
        z['investment']+=inv; z['payout']+=pay; z['profit']+=pay-inv; z['races']+=1; z['wins']+=1 if r.get('hit') else 0
    return {'races':len(rows),'settled':settled,'wins':wins,'hit_rate':hit_rate,'investment':bets,'payout':payouts,'profit':profit,'roi':roi,'current_losing_streak':current,'max_losing_streak':max_loss,'by_month':by_month}

def get(url):
    r=requests.get(url,headers=HEAD,timeout=20); r.raise_for_status(); r.encoding=r.apparent_encoding or 'utf-8'; return r.text

def num(s):
    m=re.search(r'-?\d+(?:\.\d+)?',str(s).replace(',',''))
    return float(m.group()) if m else None

def boats_from(html):
    soup=BeautifulSoup(html,'html.parser'); out={}
    for tr in soup.find_all('tr'):
        cells=[x.get_text(' ',strip=True) for x in tr.find_all(['th','td'])]
        if cells:
            m=re.match(r'^([1-6])(?:\s|$)',cells[0])
            if m:
                b=int(m.group(1)); out[b]=max(out.get(b,[]),cells,key=len)
    return out

def value(cells,labels):
    for i,c in enumerate(cells):
        if any(x in c for x in labels):
            for x in cells[i+1:i+6]:
                v=num(x)
                if v is not None:return v
    return None

def parse_before(html):
    soup=BeautifulSoup(html,'html.parser'); text=soup.get_text(' ',strip=True)
    out={'wind':None,'wave':None,'air':None,'water':None,'exhibition':{},'exhibition_st':{}}
    patterns={'wind':r'風速\s*([0-9.]+)\s*m','wave':r'波高\s*([0-9.]+)\s*cm','air':r'気温\s*([0-9.]+)℃','water':r'水温\s*([0-9.]+)℃'}
    for k,p in patterns.items():
        m=re.search(p,text)
        if m: out[k]=float(m.group(1))
    for tr in soup.find_all('tr'):
        cells=[x.get_text(' ',strip=True) for x in tr.find_all(['th','td'])]
        if not cells: continue
        m=re.match(r'^([1-6])(?:\s|$)',cells[0])
        if not m: continue
        b=int(m.group(1))
        ex=next((num(x) for x in cells if re.fullmatch(r'6\.\d{2}',x)),None)
        if ex is not None: out['exhibition'][b]=ex
        sts=[float(x.lstrip('.'))/100 for x in cells if re.fullmatch(r'\.?\d{2}',x) and not x.startswith('F')]
        if sts: out['exhibition_st'][b]=sts[-1]
    return out

def parse_resultlist(html):
    soup=BeautifulSoup(html,'html.parser'); rows=[]
    for tr in soup.find_all('tr'):
        cells=[x.get_text(' ',strip=True) for x in tr.find_all(['th','td'])]
        if not cells: continue
        m=re.match(r'^(\d{1,2})R$',cells[0])
        if not m: continue
        txt=' '.join(cells)
        tri=re.search(r'([1-6])\s*[-－]\s*([1-6])\s*[-－]\s*([1-6])',txt)
        if tri and len(set(tri.groups()))==3:
            payout=re.search(r'¥\s*([0-9,]+)',txt)
            rows.append({'race':int(m.group(1)),'combo':''.join(tri.groups()),'payout':int(payout.group(1).replace(',','')) if payout else None})
    return rows

def historical_stats(jcd,days=30):
    days=max(1,min(int(days),90)); end=datetime.now().date(); start=end-timedelta(days=days-1)
    first=[0]*7; combo={}; races=0; payouts=[]; dates=0
    d=start
    while d<=end:
        url=f'{BASE}resultlist?hd={d.strftime("%Y%m%d")}&jcd={jcd}'
        try:
            rs=parse_resultlist(get(url))
            if rs:
                dates+=1
                for r in rs:
                    races+=1; a,b,c=map(int,r['combo']); first[a]+=1; combo[r['combo']]=combo.get(r['combo'],0)+1
                    if r['payout'] is not None: payouts.append(r['payout'])
        except Exception:
            pass
        d+=timedelta(days=1)
    rates={str(i):round(first[i]/races*100,2) if races else 0 for i in range(1,7)}
    top_combos=sorted(combo.items(),key=lambda x:x[1],reverse=True)[:10]
    return {'days':days,'dates':dates,'races':races,'first_win_rate':rates,'top_combos':top_combos,'avg_payout':round(sum(payouts)/len(payouts)) if payouts else None,'max_payout':max(payouts) if payouts else None}

def analyze(raw,fixed,before,hist):
    rows=[]
    for b in range(1,7):
        c=raw.get(b,[])
        rows.append({'boat':b,'nation':value(c,['全国']),'local':value(c,['当地']),'motor':value(c,['モーター']),'st':value(c,['平均ST']), 'exhibition':before['exhibition'].get(b),'exhibition_st':before['exhibition_st'].get(b)})
    def norm(vals,x,rev=False):
        v=[z for z in vals if isinstance(z,(int,float))]
        if x is None or len(v)<2 or max(v)==min(v): return 50
        z=(x-min(v))/(max(v)-min(v))*100
        return 100-z if rev else z
    for key,rev in [('nation',0),('local',0),('motor',0),('st',1),('exhibition',1),('exhibition_st',1)]:
        vals=[r[key] for r in rows]
        for r in rows:r[key+'s']=norm(vals,r[key],rev)
    for r in rows:
        hist_rate=hist['first_win_rate'].get(str(r['boat']),0)
        # 履歴は直近データの補正として最大±10点。履歴が少ない場合は中立寄り。
        hist_adj=max(-10,min(10,(hist_rate-16.67)*0.65)) if hist['races']>=12 else 0
        r['history_rate']=hist_rate; r['history_adjustment']=round(hist_adj,1)
        mw=load_model()['weights']
        r['score']=round(r['nations']*mw['nation']+r['locals']*mw['local']+r['motors']*mw['motor']+r['sts']*mw['st']+r['exhibitions']*mw['exhibition']+r['exhibition_sts']*mw['exhibition_st']+hist_adj,1)
        if fixed!='none' and r['boat']==int(fixed): r['score']+=8
    return rows

def scenario(boats,before):
    top=max(boats,key=lambda x:x['score'])
    wind=before.get('wind') or 0
    if top['boat']==1 and wind<=4:return '逃げ'
    center=max(boats[2:4],key=lambda x:x['score'])
    if center['score']>=boats[0]['score']-4:return 'まくり・まくり差し'
    return '差し'

def build_bets(boats,odds,fixed):
    score={x['boat']:x['score'] for x in boats}; combos=[]
    for a,b,c in permutations(range(1,7),3):
        if fixed!='none' and a!=int(fixed):continue
        raw=max(score[a],1)*max(score[b],1)*max(score[c],1)
        key=f'{a}{b}{c}'; x={'bet':f'{a}-{b}-{c}','raw':raw,'odds':odds.get(key)}; combos.append(x)
    total=sum(x['raw'] for x in combos) or 1
    for x in combos:
        x['probability']=x['raw']/total
        x['ev']=None if x['odds'] is None else x['probability']*x['odds']
        x['judgement']='オッズ未取得' if x['ev'] is None else ('候補' if x['ev']>=1 else ('慎重' if x['ev']>=.8 else '見送り'))
    combos.sort(key=lambda x:x['ev'] if x['ev'] is not None else -1,reverse=True); return combos



@app.get('/api/performance')
def api_performance():
    rows=load_ledger(); return jsonify({'ok':True,'stats':ledger_stats(rows),'records':rows[-100:]})

@app.post('/api/performance')
def api_performance_add():
    data=request.get_json(force=True)
    try:
        inv=float(data.get('investment',0) or 0); payout=float(data.get('payout',0) or 0)
        if inv < 0 or payout < 0: raise ValueError('投資額・払戻は0以上で入力してください')
        combo=str(data.get('combo','')).replace('-','')
        actual=str(data.get('actual_combo','')).replace('-','')
        if actual and (len(actual)!=3 or not actual.isdigit()): raise ValueError('実結果3連単は例: 123 の形式で入力してください')
        row={'id':datetime.now().strftime('%Y%m%d%H%M%S%f'),'date':str(data.get('date') or datetime.now().strftime('%Y%m%d')).replace('/',''),'stadium':str(data.get('stadium','15')),'race':int(data.get('race',1)),'combo':combo,'actual_combo':actual,'investment':inv,'payout':payout,'profit':payout-inv,'hit':bool(actual and combo==actual),'settled':True,'note':str(data.get('note',''))[:300]}
        rows=load_ledger(); rows.append(row); save_ledger(rows)
        return jsonify({'ok':True,'record':row,'stats':ledger_stats(rows)})
    except Exception as e: return jsonify({'ok':False,'error':str(e)}),400

@app.delete('/api/performance')
def api_performance_delete():
    rows=load_ledger(); save_ledger([]); return jsonify({'ok':True,'stats':ledger_stats([])})

@app.get('/api/model')
def api_model():
    s=load_model(); return jsonify({'ok':True,'model':s})

@app.post('/api/learn')
def api_learn():
    data=request.get_json(force=True)
    predicted=[int(x) for x in data.get('predicted_order',[])][:6]
    actual=int(data.get('actual_first'))
    if not predicted or actual not in range(1,7): return jsonify({'ok':False,'error':'予測順位と実際の1着艇を指定してください'}),400
    s,hit=learn_from_record(load_model(),predicted,actual)
    return jsonify({'ok':True,'hit':bool(hit),'model':s})

@app.get('/api/backtest')
def api_backtest():
    jcd=request.args.get('stadium','15'); days=int(request.args.get('days','30')); topn=max(1,min(int(request.args.get('topn','3')),12))
    h=historical_stats(jcd,days)
    total=h['races']; combos=h['top_combos']
    # Historical-frequency benchmark: use the most frequent combinations as a transparent baseline.
    hit_rate=(sum(v for _,v in combos[:topn])/total*100) if total else 0
    return jsonify({'ok':True,'venue':STADIUMS.get(jcd,jcd),'days':days,'races':total,'top_n':topn,'benchmark_hit_rate':round(hit_rate,2),'note':'過去の3連単出現頻度を使ったベンチマークです。現在のAIモデルの的中率を意味しません。','top_combos':combos})

@app.get('/')
def home():return send_from_directory('static','index.html')

@app.get('/api/history')
def api_history():
    jcd=request.args.get('stadium','15'); days=int(request.args.get('days','30'))
    return jsonify({'ok':True,'venue':STADIUMS.get(jcd,jcd),'stats':historical_stats(jcd,days)})

@app.get('/api/analyze')
def api_analyze():
    date=request.args.get('date',datetime.now().strftime('%Y%m%d')).replace('/',''); jcd=request.args.get('stadium','15'); race=int(request.args.get('race','9')); fixed=request.args.get('fixed','none'); days=int(request.args.get('history_days','30'))
    source=f'{BASE}racelist?hd={date}&jcd={jcd}&rno={race:02d}'; before_source=f'{BASE}beforeinfo?hd={date}&jcd={jcd}&rno={race:02d}'; odds_source=f'{BASE}odds3t?hd={date}&jcd={jcd}&rno={race:02d}'
    try:
        hist=historical_stats(jcd,days); before=parse_before(get(before_source)); boats=analyze(boats_from(get(source)),fixed,before,hist); odds=parse_odds(get(odds_source)); combos=build_bets(boats,odds,fixed); boats.sort(key=lambda x:x['score'],reverse=True)
        return jsonify({'ok':True,'venue':STADIUMS.get(jcd,jcd),'boats':boats,'main':boats[0]['boat'],'second':boats[1]['boat'],'hole':boats[2]['boat'],'scenario':scenario(boats,before),'bets':combos[:12],'history':hist,'weather':{'wind':before['wind'],'wave':before['wave'],'air':before['air'],'water':before['water']},'odds_count':sum(v is not None for v in odds.values()),'notice':f'公式出走表・直前情報・公式3連単オッズに加え、直近{days}日・{hist["races"]}レースの場別結果を補正に使用しています。','source':source,'before_source':before_source,'odds_source':odds_source})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e),'source':source,'before_source':before_source,'odds_source':odds_source}),502

if __name__=='__main__':app.run(host='0.0.0.0',port=8000)
