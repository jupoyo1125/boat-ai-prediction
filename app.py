from flask import Flask, jsonify, request, send_from_directory
from itertools import permutations
from datetime import datetime, timedelta
from pathlib import Path
import json
import os
import re, requests, math, time
from bs4 import BeautifulSoup
from odds_parser import parse_odds
from model import (
    load as load_model,
    save as save_model,
    learn_from_record,
    learn_from_features,
)

app = Flask(__name__, static_folder='static')

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    return response

@app.route('/health', methods=['GET', 'OPTIONS'])
def health():
    return jsonify({'ok': True, 'service': 'boat-ai-api-v2', 'status': 'live'})

BASE = 'https://www.boatrace.jp/owpc/pc/race/'
HEAD = {'User-Agent': 'Mozilla/5.0 (compatible; BOAT-AI/4.0)'}
LEDGER = Path('performance_ledger.json')
FEATURE_KEYS = ['nation', 'local', 'motor', 'st', 'exhibition', 'exhibition_st', 'history']
def _database_url():
    return os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL')


def _db_enabled():
    return bool(_database_url())


def _db_connect():
    import psycopg
    return psycopg.connect(_database_url())


def _ensure_ledger_table():
    if not _db_enabled():
        return

    with _db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS performance_ledger (
                id TEXT PRIMARY KEY,
                record JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.commit()
STADIUMS = {
    '01':'桐生','02':'戸田','03':'江戸川','04':'平和島','05':'多摩川','06':'浜名湖',
    '07':'蒲郡','08':'常滑','09':'津','10':'三国','11':'びわこ','12':'住之江',
    '13':'尼崎','14':'鳴門','15':'丸亀','16':'児島','17':'宮島','18':'徳山',
    '19':'下関','20':'若松','21':'芦屋','22':'福岡','23':'唐津','24':'大村'
}
def normalize_date(value):
    return str(value or datetime.now().strftime('%Y%m%d')).replace('/', '').replace('-', '')
    
def get_active_stadiums(date):
    """
    指定日の公式BOAT RACE開催場を取得する
    公式ページ上の競走場別リンク(jcd)から判定する
    """
    date = normalize_date(date)

    url = f'https://www.boatrace.jp/owpc/pc/race/index?hd={date.replace("-", "")}'
    html = get(url)

    soup = BeautifulSoup(html, 'html.parser')

    active_jcd = set()

    # 公式ページ内の競走場別リンクを確認
    for link in soup.find_all('a', href=True):

        href = link.get('href', '')

        for jcd in STADIUMS.keys():

            href_decoded = href.replace('%3D', '=')

            if (
                f'?jcd={jcd}' in href_decoded
                or f'&jcd={jcd}' in href_decoded
            ):
                active_jcd.add(jcd)

    active = []

    for jcd, name in STADIUMS.items():

        if jcd in active_jcd:
            active.append({
                'stadium': jcd,
                'venue': name
            })

    return active
@app.get('/api/schedule')
def api_schedule():

    date = normalize_date(
        request.args.get('date')
    )

    try:

        venues = get_active_stadiums(date)

        return jsonify({
            'ok': True,
            'date': date,
            'venues': venues,
            'count': len(venues)
        })

    except Exception as e:

        return jsonify({
            'ok': False,
            'date': date,
            'error': (
                '開催情報を取得できませんでした: '
                f'{type(e).__name__}: {e}'
            )
        }), 502
def load_ledger():
    if _db_enabled():
        try:
            _ensure_ledger_table()

            with _db_connect() as conn:
                db_rows = conn.execute(
                    "SELECT record FROM performance_ledger ORDER BY id"
                ).fetchall()

            rows = []

            for item in db_rows:
                record = item[0]

                if isinstance(record, str):
                    record = json.loads(record)

                rows.append(record)

            # 既存のローカル台帳があれば初回だけPostgreSQLへ移行
            if not rows and LEDGER.exists():
                try:
                    legacy = json.loads(
                        LEDGER.read_text(encoding='utf-8')
                    )
                except Exception:
                    legacy = []

                if legacy:
                    save_ledger(legacy)
                    return legacy

            return rows

        except Exception:
            # DB接続に失敗した場合はローカル保存へフォールバック
            pass

    if not LEDGER.exists():
        return []

    try:
        return json.loads(
            LEDGER.read_text(encoding='utf-8')
        )

    except Exception:
        return []


def save_ledger(rows):
    rows = rows or []

    if _db_enabled():
        try:
            _ensure_ledger_table()

            with _db_connect() as conn:
                conn.execute(
                    "DELETE FROM performance_ledger"
                )

                for row in rows:
                    row_id = str(
                        row.get('id')
                        or datetime.now().strftime('%Y%m%d%H%M%S%f')
                    )

                    conn.execute(
                        """
                        INSERT INTO performance_ledger
                        (id, record)
                        VALUES (%s, %s::jsonb)
                        ON CONFLICT (id)
                        DO UPDATE SET record = EXCLUDED.record
                        """,
                        (
                            row_id,
                            json.dumps(
                                row,
                                ensure_ascii=False
                            ),
                        ),
                    )

                conn.commit()

            # DB保存成功後は旧ローカル台帳を削除
            if LEDGER.exists():
                try:
                    LEDGER.unlink()
                except Exception:
                    pass

            return

        except Exception:
            # DB接続に失敗した場合はローカル保存
            pass

    LEDGER.write_text(
        json.dumps(
            rows,
            ensure_ascii=False,
            indent=2
        ),
        encoding='utf-8'
    )
def ledger_stats(rows):
    bets = sum(float(r.get('investment', 0) or 0) for r in rows)
    payouts = sum(float(r.get('payout', 0) or 0) for r in rows)
    profit = payouts - bets
    wins = sum(1 for r in rows if r.get('hit'))
    settled = sum(1 for r in rows if r.get('settled', True))
    current = 0
    max_loss = 0
    for r in rows:
        if not r.get('settled', True):
            continue
        if r.get('hit'):
            current = 0
        else:
            current += 1
            max_loss = max(max_loss, current)
    roi = (payouts / bets * 100) if bets else None
    hit_rate = (wins / settled * 100) if settled else None
    by_month = {}
    for r in rows:
        key = str(r.get('date', ''))[:6] or 'unknown'
        z = by_month.setdefault(key, {'investment':0,'payout':0,'profit':0,'races':0,'wins':0})
        inv = float(r.get('investment', 0) or 0)
        pay = float(r.get('payout', 0) or 0)
        z['investment'] += inv
        z['payout'] += pay
        z['profit'] += pay - inv
        z['races'] += 1
        z['wins'] += 1 if r.get('hit') else 0
    return {
        'races': len(rows), 'settled': settled, 'wins': wins,
        'hit_rate': hit_rate, 'investment': bets, 'payout': payouts,
        'profit': profit, 'roi': roi,
        'current_losing_streak': current,
        'max_losing_streak': max_loss,
        'by_month': by_month
    }

def get(url):
    r = requests.get(url, headers=HEAD, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or 'utf-8'
    return r.text

def num(s):
    m = re.search(r'-?\d+(?:\.\d+)?', str(s).replace(',', ''))
    return float(m.group()) if m else None

def boat_no(text):
    s = str(text).strip()

    try:
        s = s.encode("latin1").decode("utf-8")
    except Exception:
        pass

    s = s.translate(
        str.maketrans(
            '０１２３４５６７８９',
            '0123456789'
        )
    )

    m = re.match(r'^([1-6])(?:\s|$)', s)

    return int(m.group(1)) if m else None
def boats_from(html):
    soup = BeautifulSoup(html, 'html.parser')
    out = {}
    for tr in soup.find_all('tr'):
        cells = [x.get_text(' ', strip=True) for x in tr.find_all(['th','td'])]
        if cells:
            b = boat_no(cells[0])
            if b:
                out[b] = max(out.get(b, []), cells, key=len)
    return out

def value(cells, labels):
    for i, c in enumerate(cells):
        if any(x in c for x in labels):
            for x in cells[i+1:i+6]:
                v = num(x)
                if v is not None:
                    return v
    return None

def parse_before(html):
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ', strip=True)
    out = {'wind':None,'wave':None,'air':None,'water':None,'exhibition':{},'exhibition_st':{}}
    patterns = {
        'wind': r'é¢¨é\s*([0-9.]+)\s*m',
        'wave': r'æ³¢é«\s*([0-9.]+)\s*cm',
        'air': r'æ°æ¸©\s*([0-9.]+)â',
        'water': r'æ°´æ¸©\s*([0-9.]+)â'
    }
    for k, p in patterns.items():
        m = re.search(p, text)
        if m:
            out[k] = float(m.group(1))
    for tr in soup.find_all('tr'):
        cells = [x.get_text(' ', strip=True) for x in tr.find_all(['th','td'])]
        if not cells:
            continue
        b = boat_no(cells[0])
        if not b:
            continue
        ex = num(cells[4]) if len(cells) > 4 else None
        if ex is not None:
            out['exhibition'][b] = ex
        sts = [float(x.lstrip('.'))/100 for x in cells if re.fullmatch(r'\.?\d{2}', x) and not x.startswith('F')]
        if sts:
            out['exhibition_st'][b] = sts[-1]
    return out

def parse_resultlist(html):
    soup = BeautifulSoup(html, 'html.parser')
    rows = []

    for tr in soup.find_all('tr'):
        cells = [
            x.get_text(' ', strip=True)
            for x in tr.find_all(['th', 'td'])
        ]

        if not cells:
            continue

        # 先頭セルからレース番号を取得
        m = re.match(r'^(\d{1,2})R$', cells[0].strip())
        if not m:
            continue

        race_no = int(m.group(1))
        txt = ' '.join(cells)

        # 全角記号を半角に統一
        txt = (
            txt.replace('－', '-')
               .replace('−', '-')
               .replace('―', '-')
               .replace('ー', '-')
               .replace('￥', '¥')
        )

        # 3連単の組み合わせを取得
        tri = re.search(
            r'([1-6])\s*-\s*([1-6])\s*-\s*([1-6])',
            txt
        )

        if not tri:
            continue

        combo = ''.join(tri.groups())

        # 同じ艇が重複している組み合わせは除外
        if len(set(combo)) != 3:
            continue

        # 払戻金を取得
        payout_match = re.search(
            r'[¥\u00A5]\s*([0-9][0-9,]*)',
            txt
        )

        payout = None

        if payout_match:
            payout = int(
                payout_match.group(1).replace(',', '')
            )

        rows.append({
            'race': race_no,
            'combo': combo,
            'payout': payout
        })

    return rows
def historical_stats(jcd, days=30):
    days = max(1, min(int(days), 90))
    end = datetime.now().date()
    start = end - timedelta(days=days-1)
    first = [0] * 7
    combo = {}
    races = 0
    payouts = []
    dates = 0
    d = start
    while d <= end:
        url = f'{BASE}resultlist?hd={d.strftime("%Y%m%d")}&jcd={jcd}'
        try:
            rs = parse_resultlist(get(url))
            if rs:
                dates += 1
                for r in rs:
                    races += 1
                    a, b, c = map(int, r['combo'])
                    first[a] += 1
                    combo[r['combo']] = combo.get(r['combo'], 0) + 1
                    if r['payout'] is not None:
                        payouts.append(r['payout'])
        except Exception:
            pass
        d += timedelta(days=1)
    rates = {str(i): round(first[i] / races * 100, 2) if races else 0 for i in range(1,7)}
    top_combos = sorted(combo.items(), key=lambda x:x[1], reverse=True)[:10]
    return {
        'days': days, 'dates': dates, 'races': races,
        'first_win_rate': rates, 'top_combos': top_combos,
        'avg_payout': round(sum(payouts)/len(payouts)) if payouts else None,
        'max_payout': max(payouts) if payouts else None
    }

def analyze(raw, fixed, before, hist):
    rows = []
    for b in range(1, 7):
        c = raw.get(b, [])
        # 公式出走表は列位置で取得する
        name_text = c[2] if len(c) > 2 else ''
        name_text = re.sub(
            r'^\d+\s*/\s*[A-Z]\d+\s*',
            '',
            name_text
        ).strip()

        st_nums = re.findall(
            r'\d+(?:\.\d+)?',
            c[3] if len(c) > 3 else ''
        )

        nation_nums = re.findall(
            r'\d+(?:\.\d+)?',
            c[4] if len(c) > 4 else ''
        )

        local_nums = re.findall(
            r'\d+(?:\.\d+)?',
            c[5] if len(c) > 5 else ''
        )

        motor_nums = re.findall(
            r'\d+(?:\.\d+)?',
            c[6] if len(c) > 6 else ''
        )

        rows.append({
            'boat': b,
            'name': name_text,
            'nation': float(nation_nums[0]) if nation_nums else None,
            'local': float(local_nums[0]) if local_nums else None,
            'motor': float(motor_nums[1]) if len(motor_nums) > 1 else None,
            'st': float(st_nums[-1]) if st_nums else None,
            'exhibition': before['exhibition'].get(b),
            'exhibition_st': before['exhibition_st'].get(b)
        })
    def norm(vals, x, rev=False):
        v = [z for z in vals if isinstance(z, (int, float))]
        if x is None or len(v) < 2 or max(v) == min(v):
            return 50
        z = (x - min(v)) / (max(v) - min(v)) * 100
        return 100 - z if rev else z

    for key, rev in [
        ('nation', False), ('local', False), ('motor', False),
        ('st', True), ('exhibition', True), ('exhibition_st', True)
    ]:
        vals = [r[key] for r in rows]
        for r in rows:
            r[key + 's'] = round(norm(vals, r[key], rev), 2)

    model = load_model()
    mw = model.get('weights', {})
    for r in rows:
        hist_rate = hist['first_win_rate'].get(str(r['boat']), 0)
        hist_norm = max(0, min(100, 50 + (hist_rate - 16.67) * 4))
        r['history_rate'] = hist_rate
        r['history_adjustment'] = round(hist_norm - 50, 2)
        r['historys'] = round(hist_norm, 2)

        r['score'] = round(
            r['nations'] * mw.get('nation', .28) +
            r['locals'] * mw.get('local', .11) +
            r['motors'] * mw.get('motor', .17) +
            r['sts'] * mw.get('st', .18) +
            r['exhibitions'] * mw.get('exhibition', .08) +
            r['exhibition_sts'] * mw.get('exhibition_st', .03) +
            r['historys'] * mw.get('history', .15),
            2
        )
        if fixed != 'none' and r['boat'] == int(fixed):
            r['score'] += 8

    return rows

def scenario(boats, before):
    top = max(boats, key=lambda x:x['score'])
    wind = before.get('wind') or 0
    if top['boat'] == 1 and wind <= 4:
        return '逃げ'
    center = max(boats[2:4], key=lambda x:x['score'])
    if center['score'] >= boats[0]['score'] - 4:
        return 'まくり・まくり差し'
    return '差し'

def _softmax(values, temperature=12.0):
    if not values:
        return []
    t = max(float(temperature), 0.1)
    m = max(values)
    ex = [math.exp((v-m)/t) for v in values]
    s = sum(ex) or 1.0
    return [x/s for x in ex]

def build_bets(boats, odds, fixed):
    scores = {x['boat']: x['score'] for x in boats}
    temperature = float(load_model().get('temperature', 12.0))

    combos = []

    for a, b, c in permutations(range(1, 7), 3):
        if fixed != 'none' and a != int(fixed):
            continue

        remaining = [x for x in range(1, 7) if x != a]

        p1 = _softmax(
            [scores[x] for x in range(1, 7)],
            temperature
        )[a - 1]

        p2 = _softmax(
            [scores[x] for x in remaining],
            temperature
        )[remaining.index(b)]

        rem2 = [x for x in remaining if x != b]

        p3 = _softmax(
            [scores[x] for x in rem2],
            temperature
        )[rem2.index(c)]

        prob = p1 * p2 * p3

        key = f'{a}{b}{c}'
        odd = odds.get(key)

        ev = None if odd is None else prob * odd

        combos.append({
            'bet': f'{a}-{b}-{c}',
            'probability': round(prob, 6),
            'odds': odd,
            'ev': round(ev, 4) if ev is not None else None,
            'judgement': (
                'オッズ未取得'
                if ev is None
                else (
                    '候補'
                    if ev >= 1
                    else (
                        '慎重'
                        if ev >= 0.8
                        else '見送り'
                    )
                )
            )
        })

    # オッズ取得済みの買い目
    available = [
        x for x in combos
        if x['odds'] is not None
    ]

    # ① ガチガチ
    # 的中確率を最優先
    gachi = sorted(
        available,
        key=lambda x: (
            x['probability'],
            x['ev'] if x['ev'] is not None else -1
        ),
        reverse=True
    )[:10]

    used = {
        x['bet']
        for x in gachi
    }

    # ② ロマン砲
    # 高オッズを優先しつつ、極端に確率が低い買い目を避ける
    roman_pool = [
        x for x in available
        if x['bet'] not in used
        and x['probability'] >= 0.002
    ]

    roman = sorted(
        roman_pool,
        key=lambda x: (
            x['odds'],
            x['ev'] if x['ev'] is not None else -1
        ),
        reverse=True
    )[:10]

    used.update(
        x['bet']
        for x in roman
    )

    # ③ 鬼しぼり
    # 期待値(EV)を最優先
    oni_pool = [
        x for x in available
        if x['bet'] not in used
    ]

    oni = sorted(
        oni_pool,
        key=lambda x: (
            x['ev'] if x['ev'] is not None else -1,
            x['probability']
        ),
        reverse=True
    )[:3]

    # カテゴリを付与
    for x in gachi:
        x['category'] = 'gachi'

    for x in roman:
        x['category'] = 'roman'

    for x in oni:
        x['category'] = 'oni'

    # ガチガチ → ロマン砲 → 鬼しぼり
    return gachi + roman + oni
def feature_snapshot(boats):
    return {
        str(r['boat']): {
            'nation': r.get('nations', 50),
            'local': r.get('locals', 50),
            'motor': r.get('motors', 50),
            'st': r.get('sts', 50),
            'exhibition': r.get('exhibitions', 50),
            'exhibition_st': r.get('exhibition_sts', 50),
            'history': r.get('historys', 50),
        }
        for r in boats
    }

@app.get('/api/performance')
def api_performance():
    rows = load_ledger()
    return jsonify({'ok': True, 'stats': ledger_stats(rows), 'records': rows[-100:]})
@app.get('/api/saved_prediction')
def api_saved_prediction():
    date = str(request.args.get('date','')).replace('/','').replace('-','')
    stadium = str(request.args.get('stadium',''))
    race = int(request.args.get('race','1'))

    rows = load_ledger()

    candidates = [
        r for r in rows
        if str(r.get('date','')).replace('/','').replace('-','') == date
        and str(r.get('stadium','')) == stadium
        and int(r.get('race',0) or 0) == race
    ]

    if not candidates:
        return jsonify({
            'ok': False,
            'error': '保存済みAI予想がありません'
        }), 404

    row = candidates[-1]

    return jsonify({
        'ok': True,
        'record': row,
        'bets': row.get('bets') or []
    })

@app.post('/api/performance')
def api_performance_add():
    data = request.get_json(force=True)
    try:
        inv = float(data.get('investment', 0) or 0)
        payout = float(data.get('payout', 0) or 0)
        if inv < 0 or payout < 0:
            raise ValueError('æè³é¡ã»ææ»ã¯0ä»¥ä¸ã§å¥åãã¦ãã ãã')
        combo = str(data.get('combo', '')).replace('-', '')
        actual = str(data.get('actual_combo', '')).replace('-', '')
        if actual and (len(actual) != 3 or not actual.isdigit()):
            raise ValueError('å®çµæ3é£åã¯ä¾: 123 ã®å½¢å¼ã§å¥åãã¦ãã ãã')
        row = {
            'id': datetime.now().strftime('%Y%m%d%H%M%S%f'),
            'date': str(data.get('date') or datetime.now().strftime('%Y%m%d')).replace('/',''),
            'stadium': str(data.get('stadium','15')),
            'race': int(data.get('race',1)),
            'combo': combo,
            'actual_combo': actual,
            'investment': inv,
            'payout': payout,
            'profit': payout-inv,
            'hit': bool(actual and combo == actual),
            'settled': bool(actual),
            'learned': False,
            'predicted_first': int(data.get('predicted_first')) if str(data.get('predicted_first','')).isdigit() else None,
            'features': data.get('features') or {},
            'bets': data.get('bets') or [],
            'note': str(data.get('note',''))[:300]
        }
        rows = load_ledger()

        # 同じ日付・場・レースの重複保存を防止
        same_race = [
            r for r in rows
            if str(r.get('date', '')) == str(row.get('date', ''))
            and str(r.get('stadium', '')) == str(row.get('stadium', ''))
            and int(r.get('race', 0)) == int(row.get('race', 0))
        ]

        if same_race:
            # 未学習の記録があれば、それを更新
            unlearned = [
                r for r in same_race
                if not r.get('learned', False)
            ]

            if unlearned:
                existing = unlearned[-1]
                existing.update(row)
                save_ledger(rows)

                return jsonify({
                    'ok': True,
                    'record': existing,
                    'duplicate': True
                })

            # 過去の記録がすべて学習済みなら、
            # 新しい予想として追加保存する
            rows.append(row)
            save_ledger(rows)

            return jsonify({
                'ok': True,
                'record': row,
                'duplicate': False
            })
        rows.append(row)
        save_ledger(rows)
        return jsonify({'ok': True, 'record': row, 'stats': ledger_stats(rows)})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)}), 400

@app.delete('/api/performance')
def api_performance_delete():
    rows = load_ledger()
    save_ledger([])
    return jsonify({'ok':True,'stats':ledger_stats([])})

@app.get('/api/model')
def api_model():
    return jsonify({'ok':True,'model':load_model()})

@app.post('/api/learn')
def api_learn():
    data = request.get_json(force=True)
    predicted = [int(x) for x in data.get('predicted_order',[])][:6]
    actual = int(data.get('actual_first'))
    if not predicted or actual not in range(1,7):
        return jsonify({'ok':False,'error':'äºæ¸¬é ä½ã¨å®éã®1çèãæå®ãã¦ãã ãã'}),400
    s, hit = learn_from_record(load_model(), predicted, actual)
    return jsonify({'ok':True,'hit':bool(hit),'model':s})

@app.post('/api/settle_prediction')
def api_settle_prediction():
    data = request.get_json(force=True)

    date = str(
        data.get('date') or datetime.now().strftime('%Y%m%d')
    ).replace('/', '').replace('-', '')

    jcd = str(data.get('stadium', '15'))
    race = int(data.get('race', 1))

    # 指定レースの公式結果ページを直接取得
    result_source = (
        f'{BASE}raceresult?hd={date}&jcd={jcd}&rno={race}'
    )

    try:
        result_html = get(result_source)
        soup = BeautifulSoup(result_html, 'html.parser')

        # ページ全体の文字を取得
        text = soup.get_text(' ', strip=True)

        # 記号を半角に統一
        text = (
            text.replace('－', '-')
                .replace('−', '-')
                .replace('―', '-')
                .replace('ー', '-')
                .replace('￥', '¥')
        )

        # 3連単の結果を取得
        tri_match = re.search(
            r'3連単\s*([1-6])\s*-\s*([1-6])\s*-\s*([1-6])',
            text
        )

        if not tri_match:
            return jsonify({
                'ok': False,
                'error': '指定レースの3連単結果を取得できませんでした。',
                'source': result_source
            }), 404

        combo = ''.join(tri_match.groups())

        # 同じ艇が重複していないか確認
        if len(set(combo)) != 3:
            return jsonify({
                'ok': False,
                'error': '取得した3連単結果が不正です。',
                'source': result_source
            }), 502

        # 3連単の直後にある払戻金を取得
        payout_match = re.search(
            r'3連単\s*[1-6]\s*-\s*[1-6]\s*-\s*[1-6]'
            r'.{0,100}?¥\s*([0-9][0-9,]*)',
            text
        )

        official_payout = 0

        if payout_match:
            official_payout = int(
                payout_match.group(1).replace(',', '')
            )

        result = {
            'race': race,
            'combo': combo,
            'payout': official_payout
        }

        # 保存済みAI予想を取得
        rows = load_ledger()

        candidates = [
            r for r in rows
            if str(r.get('date', '')).replace('/', '').replace('-', '') == date
            and str(r.get('stadium', '')) == jcd
            and int(r.get('race', 0) or 0) == race
            and not r.get('learned', False)
        ]

        if not candidates:
            return jsonify({
                'ok': False,
                'error': '未学習の予想記録が見つかりません。先にAI予想を実行してください。',
                'result': result,
                'source': result_source
            }), 404

        row = candidates[-1]

        # 実際の結果を保存
        actual_combo = result['combo']
        row['actual_combo'] = actual_combo

        # 的中判定
        row['hit'] = bool(
            row.get('combo') == actual_combo
        )

        investment = float(
            row.get('investment', 0) or 0
        )

        # 払戻計算
        if row['hit'] and investment > 0:
            row['payout'] = round(
                official_payout * (investment / 100.0),
                2
            )
        else:
            row['payout'] = 0

        row['official_payout'] = official_payout
        row['profit'] = (
            float(row.get('payout', 0) or 0)
            - investment
        )

        row['settled'] = True

        # 学習処理
        actual_first = int(actual_combo[0])
        predicted_first = row.get('predicted_first')
        features = row.get('features') or {}

        predicted_features = {}

        if (
            isinstance(features, dict)
            and predicted_first in range(1, 7)
        ):
            predicted_features = (
                features.get(str(predicted_first))
                or features.get(predicted_first)
                or {}
            )

        if predicted_features:
            actual_features = (
                features.get(str(actual_first))
                or features.get(actual_first)
                or {}
            )

            state, hit = learn_from_features(
                load_model(),
                predicted_features,
                actual_first,
                predicted_first,
                actual_features
            )

        else:
            predicted_order = [
                int(x)
                for x in str(row.get('combo', ''))
                if x.isdigit()
            ]

            state, hit = learn_from_record(
                load_model(),
                predicted_order,
                actual_first
            )

        row['learned'] = True
        row['learn_hit'] = bool(hit)

        # データを保存
        save_ledger(rows)

        return jsonify({
            'ok': True,
            'result': result,
            'record': row,
            'learning_hit': bool(hit),
            'model': state,
            'source': result_source
        })

    except Exception as e:
        return jsonify({
            'ok': False,
            'error': str(e),
            'source': result_source
        }), 502
@app.get('/api/backtest')
def api_backtest():
    jcd = request.args.get('stadium','15')
    days = int(request.args.get('days','30'))
    topn = max(1,min(int(request.args.get('topn','3')),12))
    h = historical_stats(jcd,days)
    total = h['races']
    combos = h['top_combos']
    hit_rate = (sum(v for _,v in combos[:topn])/total*100) if total else 0
    return jsonify({
        'ok':True,'venue':STADIUMS.get(jcd,jcd),'days':days,'races':total,
        'top_n':topn,'benchmark_hit_rate':round(hit_rate,2),
        'note':'éå»ã®3é£ååºç¾é »åº¦ãä½¿ã£ããã³ããã¼ã¯ã§ããç¾å¨ã®AIã¢ãã«ã®çä¸­çãæå³ãã¾ããã',
        'top_combos':combos
    })

@app.get('/')
def home():
    return send_from_directory('static','index.html')

@app.get('/api/history')
def api_history():
    jcd = request.args.get('stadium','15')
    days = int(request.args.get('days','30'))
    return jsonify({'ok':True,'venue':STADIUMS.get(jcd,jcd),'stats':historical_stats(jcd,days)})

@app.get('/api/analyze')
def api_analyze():
    date = request.args.get('date',datetime.now().strftime('%Y%m%d')).replace('/','').replace('-','')
    jcd = request.args.get('stadium','15')
    race = int(request.args.get('race','9'))
    fixed = request.args.get('fixed','none')
    days = int(request.args.get('history_days','30'))
    source = f'{BASE}racelist?hd={date}&jcd={jcd}&rno={race:02d}'
    before_source = f'{BASE}beforeinfo?hd={date}&jcd={jcd}&rno={race:02d}'
    odds_source = f'{BASE}odds3t?hd={date}&jcd={jcd}&rno={race:02d}'
    try:
        hist = historical_stats(jcd,days)
        before = parse_before(get(before_source))
        boats = analyze(boats_from(get(source)),fixed,before,hist)
        odds = parse_odds(get(odds_source))
        combos = build_bets(boats,odds,fixed)
        boats.sort(key=lambda x:x['score'],reverse=True)
        return jsonify({
            'ok':True,'venue':STADIUMS.get(jcd,jcd),'boats':boats,
            'main':boats[0]['boat'],'second':boats[1]['boat'],'hole':boats[2]['boat'],
            'scenario':scenario(boats,before),'bets':combos[:12],
            'history':hist,
            'features':feature_snapshot(boats),
            'weather':{'wind':before['wind'],'wave':before['wave'],'air':before['air'],'water':before['water']},
            'odds_count':sum(v is not None for v in odds.values()),
            'notice':f'å¬å¼åºèµ°è¡¨ã»ç´åæå ±ã»å¬å¼3é£åãªããºã«å ããç´è¿{days}æ¥ã»{hist["races"]}ã¬ã¼ã¹ã®å ´å¥çµæãè£æ­£ã«ä½¿ç¨ãã¦ãã¾ãã',
            'source':source,'before_source':before_source,'odds_source':odds_source
        })
    except Exception as e:
        return jsonify({'ok':False,'error':str(e),'source':source,'before_source':before_source,'odds_source':odds_source}),502

if __name__ == '__main__':
    app.run(host='0.0.0.0',port=8000)
