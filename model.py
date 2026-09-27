import json, os, math
from datetime import datetime

PATH=os.path.join(os.path.dirname(__file__),'model_state.json')
DEFAULT={'weights':{'nation':.28,'local':.11,'motor':.17,'st':.18,'exhibition':.08,'exhibition_st':.03,'history':.15},'samples':0,'hits':0,'updated_at':None}

def load():
    if not os.path.exists(PATH): return DEFAULT.copy()
    try:
        with open(PATH,'r',encoding='utf-8') as f: s=json.load(f)
        w=DEFAULT['weights'].copy(); w.update(s.get('weights',{})); s['weights']=w; return s
    except Exception: return DEFAULT.copy()

def save(s):
    s['updated_at']=datetime.now().isoformat(timespec='seconds')
    with open(PATH,'w',encoding='utf-8') as f: json.dump(s,f,ensure_ascii=False,indent=2)

def normalize_weights(w):
    w={k:max(.01,float(v)) for k,v in w.items()}
    total=sum(w.values()); return {k:round(v/total,4) for k,v in w.items()}

def learn_from_record(state, predicted, actual):
    # predicted: ordered boat ids. Actual: winning first boat.
    hit = int(predicted and predicted[0]==actual)
    state['samples']+=1; state['hits']+=hit
    # Lightweight online update: reward features associated with the predicted winner, penalize on miss.
    # This is a heuristic adaptive model, not a trained ML model.
    delta=.012 if hit else -.006
    for k in ['nation','local','motor','st','exhibition','exhibition_st','history']:
        state['weights'][k]+=delta
    state['weights']=normalize_weights(state['weights'])
    save(state); return state,hit
