from flask import Flask, jsonify, request, send_from_directory
from itertools import permutations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re, requests, math, time
import unicodedata
import sys
import threading
from bs4 import BeautifulSoup
from odds_parser import parse_odds
from model import (
    load as load_model,
    save as save_model,
    learn_from_record,
    learn_from_features,
)
from state_store import (
    atomic_state, state_atomic, state_connection, in_state_transaction,
    atomic_write_json, remember_ledger, ledger_snapshot, storage_required,
    database_url, storage_paused, require_storage_writable, StorageMaintenance,
)
from storage_migration import initialize_storage, storage_report

initialize_storage()

app = Flask(__name__, static_folder='static')

@app.before_request
def pause_storage_updates():
    if storage_paused() and request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return storage_maintenance_response(StorageMaintenance())


@app.errorhandler(StorageMaintenance)
def storage_maintenance_response(error):
    response = jsonify({'ok': False, 'code': 'storage_maintenance',
                        'error': '保存先の移行中です。更新は完了後に再開します。'})
    response.status_code = 503
    response.headers['Retry-After'] = '120'
    return response

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    return response

@app.route('/health', methods=['GET', 'OPTIONS'])
def health():
    return jsonify({'ok': True, 'service': 'boat-ai-api-v2', 'status': 'live',
                    'storage': storage_report()})

BASE = 'https://www.boatrace.jp/owpc/pc/race/'
HEAD = {'User-Agent': 'Mozilla/5.0 (compatible; BOAT-AI/4.0)'}
LEDGER = Path('performance_ledger.json')
BET_UNIT = 100
BET_CATEGORIES = ('gachi', 'roman', 'oni')
FEATURE_KEYS = ['nation', 'local', 'motor', 'st', 'exhibition', 'exhibition_st', 'history']
# Shared by manual batches and the automatic runner. Never fan out by venue.
_official_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='official')
_official_slots = threading.BoundedSemaphore(3)
_http_local = threading.local()
LEDGER_UPSERT_SQL = '''
    INSERT INTO performance_ledger (id, record)
    SELECT id, record FROM jsonb_to_recordset(%s::jsonb) AS items(id TEXT, record JSONB)
    ON CONFLICT (id) DO UPDATE SET record = EXCLUDED.record
'''


def official_session():
    if not hasattr(_http_local, 'session'):
        _http_local.session = requests.Session()
    return _http_local.session


def _database_url():
    return database_url()


def _db_enabled():
    return bool(_database_url())


def _db_connect():
    return state_connection(_database_url())


def _ensure_ledger_table():
    if not _db_enabled() or storage_paused():
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
    html = get(url, timeout=15)

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
            if not rows and LEDGER.exists() and not storage_paused():
                try:
                    legacy = json.loads(
                        LEDGER.read_text(encoding='utf-8')
                    )
                except Exception:
                    legacy = []

                if legacy:
                    save_ledger(legacy)
                    return legacy

            remember_ledger(rows)
            return rows

        except Exception:
            if in_state_transaction() or storage_required():
                raise
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
    require_storage_writable()
    rows = rows or []

    if _db_enabled():
        try:
            _ensure_ledger_table()

            with _db_connect() as conn:
                previous = ledger_snapshot()
                if previous is None:
                    previous = {
                        str(item[0]): json.dumps(item[1], sort_keys=True, ensure_ascii=False)
                        for item in conn.execute('SELECT id, record FROM performance_ledger').fetchall()
                    }
                current_ids = {str(row['id']) for row in rows if row.get('id')}
                removed = list(set(previous) - current_ids)
                if removed:
                    conn.execute('DELETE FROM performance_ledger WHERE id = ANY(%s)', (removed,))

                changed = {}
                for row in rows:
                    row_id = str(
                        row.get('id')
                        or datetime.now().strftime('%Y%m%d%H%M%S%f')
                    )

                    if previous.get(row_id) == json.dumps(row, sort_keys=True, ensure_ascii=False):
                        continue

                    changed[row_id] = {'id': row_id, 'record': row}

                if changed:
                    conn.execute(LEDGER_UPSERT_SQL,
                                 (json.dumps(list(changed.values()), ensure_ascii=False),))

                conn.commit()

            # DB保存成功後は旧ローカル台帳を削除
            if LEDGER.exists() and not in_state_transaction():
                try:
                    LEDGER.unlink()
                except Exception:
                    pass

            return

        except Exception:
            if in_state_transaction() or storage_required():
                raise
            pass

    atomic_write_json(LEDGER, rows)
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

def normalize_category_bets(bets, strict=True):
    if not isinstance(bets, list):
        if strict:
            raise ValueError('買い目は配列で指定してください。')
        return []

    result = []
    seen = set()
    for bet in bets:
        if not isinstance(bet, dict):
            if strict:
                raise ValueError('買い目の形式が不正です。')
            continue
        category = bet.get('category')
        combo = str(bet.get('bet', '')).replace('-', '')
        if (
            category not in BET_CATEGORIES
            or not re.fullmatch(r'[1-6]{3}', combo)
            or len(set(combo)) != 3
        ):
            if strict:
                raise ValueError('買い目の区分または3連単の形式が不正です。')
            continue
        key = (category, combo)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(bet, bet='-'.join(combo), investment=BET_UNIT))
    return result


def category_race_results(row):
    if row.get('performance_version') != 2:
        return {}
    bets = normalize_category_bets(row.get('bets') or [], strict=False)
    actual = str(row.get('actual_combo') or '').replace('-', '')
    settled = bool(row.get('settled')) and bool(re.fullmatch(r'[1-6]{3}', actual))
    official_payout = float(row.get('official_payout', 0) or 0)
    results = {}
    for category in BET_CATEGORIES:
        combos = [
            bet['bet'].replace('-', '')
            for bet in bets if bet['category'] == category
        ]
        investment = len(combos) * BET_UNIT
        hit = bool(settled and actual in combos)
        payout = official_payout if hit else 0
        results[category] = {
            'bet_count': len(combos),
            'investment': investment,
            'payout': payout,
            'profit': payout - investment,
            'hit': hit,
            'settled': bool(settled and combos),
        }
    return results


def apply_category_accounting(row):
    results = category_race_results(row)
    row['category_results'] = results
    row['investment'] = sum(result['investment'] for result in results.values())
    row['payout'] = sum(result['payout'] for result in results.values())
    row['profit'] = row['payout'] - row['investment']
    row['hit'] = any(result['hit'] for result in results.values())


def category_ledger_stats(rows):
    # 再分析した同じレースを重複して集計しない。
    latest = {}
    for row in rows:
        if row.get('performance_version') != 2:
            continue
        key = (
            str(row.get('date', '')).replace('/', '').replace('-', ''),
            str(row.get('stadium', '')).zfill(2),
            int(row.get('race', 0)),
        )
        if key in latest:
            del latest[key]
        latest[key] = row

    grouped = {category: [] for category in BET_CATEGORIES}
    for row in latest.values():
        for category, result in category_race_results(row).items():
            if result['bet_count']:
                grouped[category].append(dict(result, date=row.get('date', '')))

    stats = {}
    for category, records in grouped.items():
        settled = [record for record in records if record['settled']]
        stats[category] = ledger_stats(settled)
        stats[category]['pending'] = len(records) - len(settled)
        stats[category]['bet_count'] = sum(record['bet_count'] for record in settled)
    return stats


def get(url, timeout=5):
    started = time.monotonic()

    try:
        with _official_slots:
            with official_session().get(url, headers=HEAD, timeout=timeout) as r:
                r.raise_for_status()
                r.encoding = r.apparent_encoding or 'utf-8'
                return r.text

    except requests.RequestException:
        app.logger.exception(
            "External GET failed url=%s timeout=%s elapsed=%.2fs",
            url,
            timeout,
            time.monotonic() - started
        )
        raise

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
    soup = BeautifulSoup(unicodedata.normalize('NFKC', html), 'html.parser')
    text = soup.get_text(' ', strip=True)
    out = {'wind':None,'wave':None,'air':None,'water':None,'exhibition':{},'exhibition_st':{}}
    patterns = {
        'wind': r'風速\s*([0-9.]+)\s*m',
        'wave': r'波高\s*([0-9.]+)\s*cm',
        'air': r'気温\s*([0-9.]+)',
        'water': r'水温\s*([0-9.]+)'
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
        if ex is not None and 5 <= ex <= 9:
            out['exhibition'][b] = ex
    # 展示STは「前走成績」のSTと別の、スタート展示の専用欄から読む。
    for item in soup.select('.table1_boatImage1'):
        number = item.select_one('.table1_boatImage1Number')
        value_node = item.select_one('.table1_boatImage1Time')
        if number is None or value_node is None:
            continue
        b = boat_no(number.get_text(strip=True))
        st = value_node.get_text(strip=True).replace(' ', '')
        if b and re.fullmatch(r'F?\.\d{2}', st):
            out['exhibition_st'][b] = (-1 if st.startswith('F') else 1) * float(st.lstrip('F'))
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
@lru_cache(maxsize=100)
def historical_stats(jcd, days=30, end_date=None):
    days = max(1, min(int(days), 3))

    if end_date:
        end = datetime.strptime(
            str(end_date),
            '%Y%m%d'
        ).date()
    else:
        end = datetime.now().date()

    start = end - timedelta(days=days-1)

    first = [0] * 7
    combo = {}
    races = 0
    payouts = []
    dates = 0

    dates_to_fetch = [start + timedelta(days=offset) for offset in range(days)]
    pages = [_official_pool.submit(get,
                f'{BASE}resultlist?hd={day.strftime("%Y%m%d")}&jcd={jcd}', timeout=15)
             for day in dates_to_fetch]
    for page in pages:
        try:
            rs = parse_resultlist(page.result())
            if rs:
                dates += 1
                for r in rs:
                    races += 1
                    first[int(r['combo'][0])] += 1
                    combo[r['combo']] = combo.get(r['combo'], 0) + 1
                    if r['payout'] is not None:
                        payouts.append(r['payout'])
        except Exception:
            pass

    rates = {
        str(i):
        round(
            first[i] / races * 100,
            2
        ) if races else 0
        for i in range(1, 7)
    }

    top_combos = sorted(
        combo.items(),
        key=lambda x: x[1],
        reverse=True
    )[:10]

    return {
        'days': days,
        'dates': dates,
        'races': races,
        'first_win_rate': rates,
        'top_combos': top_combos,
        'avg_payout':
            round(sum(payouts) / len(payouts))
            if payouts else None,
        'max_payout':
            max(payouts)
            if payouts else None
    }

def analyze(raw, fixed, before, hist, model=None):
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

    model = load_model() if model is None else model
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

def build_bets(boats, odds, fixed, scenario_name=None, model=None):

    scores = {
        x['boat']: x['score']
        for x in boats
    }

    temperature = float(
        (load_model() if model is None else model).get('temperature', 12.0)
    )

    combos = []

    # 本命・対抗・穴
    ranking = sorted(
        boats,
        key=lambda x: x['score'],
        reverse=True
    )

    main_boat = ranking[0]['boat']
    second_boat = ranking[1]['boat']
    hole_boat = ranking[2]['boat']

    for a, b, c in permutations(range(1, 7), 3):

        if fixed != 'none' and a != int(fixed):
            continue

        remaining = [
            x for x in range(1, 7)
            if x != a
        ]

        p1 = _softmax(
            [scores[x] for x in range(1, 7)],
            temperature
        )[a - 1]

        p2 = _softmax(
            [scores[x] for x in remaining],
            temperature
        )[remaining.index(b)]

        rem2 = [
            x for x in remaining
            if x != b
        ]

        p3 = _softmax(
            [scores[x] for x in rem2],
            temperature
        )[rem2.index(c)]

        probability = p1 * p2 * p3

        key = f'{a}{b}{c}'
        odd = odds.get(key)

        ev = (
            None
            if odd is None
            else probability * odd
        )

        combos.append({
            'bet': f'{a}-{b}-{c}',
            'probability': round(
                probability,
                6
            ),
            'odds': odd,
            'ev': (
                round(ev, 4)
                if ev is not None
                else None
            ),
            'judgement': (
                'オッズ未取得'
                if ev is None
                else (
                    '◎'
                    if ev >= 1.0
                    else (
                        '○'
                        if ev >= 0.8
                        else '△'
                    )
                )
            )
        })

    available = [
        x for x in combos
        if x['odds'] is not None
    ]

    # ==================================================
    # 🔥 ガチガチ
    # ==================================================

    GACHI_MIN_ODDS = 2.0

    gachi_pool = [
        x for x in available
        if float(x['odds']) >= GACHI_MIN_ODDS
        and (
            int(x['bet'][0])
            in [main_boat, second_boat]
        )
        and (
            int(x['bet'][2])
            in [main_boat, second_boat, hole_boat]
            or
            int(x['bet'][4])
            in [main_boat, second_boat, hole_boat]
        )
    ]

    gachi = sorted(
        gachi_pool,
        key=lambda x: (
            x['probability'],
            x['ev'] if x['ev'] is not None else -1
        ),
        reverse=True
    )[:10]

    # 条件を満たす買い目が10点未満の場合、
    # 同じオッズ条件の中から確率順で補完
    if len(gachi) < 10:

        used = {
            x['bet']
            for x in gachi
        }

        supplement = [
            x for x in available
            if float(x['odds']) >= GACHI_MIN_ODDS
            and x['bet'] not in used
        ]

        supplement = sorted(
            supplement,
            key=lambda x: (
                x['probability'],
                x['ev'] if x['ev'] is not None else -1
            ),
            reverse=True
        )

        gachi.extend(
            supplement[
                :10-len(gachi)
            ]
        )

    # ==================================================
    # 🚀 ロマン砲
    # ==================================================

    ROMAN_MIN_ODDS = 50.0
    ROMAN_MIN_PROBABILITY = 0.002

    roman_pool = [
        x for x in available
        if float(x['odds']) >= ROMAN_MIN_ODDS
        and float(x['probability']) >= ROMAN_MIN_PROBABILITY
    ]

    def roman_priority(x):

        a = int(x['bet'][0])

        # 展開を考慮した優先順位
        scenario_bonus = 0

        if scenario_name == '逃げ':
            if a == hole_boat:
                scenario_bonus = 3
            elif a != main_boat:
                scenario_bonus = 2

        elif scenario_name in [
            '差し',
            'まくり・まくり差し'
        ]:
            if a in [second_boat, hole_boat]:
                scenario_bonus = 3
            elif a != main_boat:
                scenario_bonus = 2

        else:
            if a == hole_boat:
                scenario_bonus = 2

        return (
            scenario_bonus,
            float(x['odds']),
            float(x['probability'])
        )

    roman = sorted(
        roman_pool,
        key=roman_priority,
        reverse=True
    )[:10]

    # ==================================================
    # 👹 鬼しぼり
    # ==================================================

    ONI_MIN_EV = 1.0

    oni_pool = [
        x for x in available
        if x['ev'] is not None
        and float(x['ev']) >= ONI_MIN_EV
    ]

    def oni_score(x):

        probability = float(
            x['probability']
        )

        odds_value = float(
            x['odds']
        )

        ev_value = float(
            x['ev']
        )

        # EVを中心に、
        # 確率とオッズも加味する
        score = (
            ev_value * 0.50
            +
            probability * 100 * 0.30
            +
            min(odds_value / 100, 10) * 0.20
        )

        return score

    oni = sorted(
        oni_pool,
        key=lambda x: (
            oni_score(x),
            x['ev'],
            x['probability']
        ),
        reverse=True
    )[:3]

    # 同じ買い目が別区分に含まれても、区分名を上書きしない。
    return (
        [dict(x, category='gachi') for x in gachi]
        + [dict(x, category='roman') for x in roman]
        + [dict(x, category='oni') for x in oni]
    )
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
    return jsonify({
        'ok': True,
        'stats': ledger_stats(rows),
        'category_stats': category_ledger_stats(rows),
        'automatic_category_stats': category_ledger_stats([
            row for row in rows if row.get('prediction_origin') == 'automatic'
        ]),
        'historical': historical_status(),
        'legacy_records': sum(1 for row in rows if row.get('performance_version') != 2),
        'records': rows[-100:],
    })
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

def performance_record(data):
    inv = float(data.get('investment', 0) or 0)
    payout = float(data.get('payout', 0) or 0)
    if inv < 0 or payout < 0:
        raise ValueError('投資額・払戻は0以上で入力してください。')
    combo = str(data.get('combo', '')).replace('-', '')
    actual = str(data.get('actual_combo', '')).replace('-', '')
    if actual and (len(actual) != 3 or not actual.isdigit()):
        raise ValueError('実結果3連単は123の形式で入力してください。')
    row = {
        'id': datetime.now().strftime('%Y%m%d%H%M%S%f'),
        'date': str(data.get('date') or datetime.now().strftime('%Y%m%d')).replace('/', ''),
        'stadium': str(data.get('stadium', '15')),
        'race': int(data.get('race', 1)),
        'combo': combo, 'actual_combo': actual,
        'investment': inv, 'payout': payout, 'profit': payout-inv,
        'hit': bool(actual and combo == actual), 'settled': bool(actual), 'learned': False,
        'predicted_first': int(data['predicted_first']) if str(data.get('predicted_first', '')).isdigit() else None,
        'features': data.get('features') or {}, 'bets': data.get('bets') or [],
        'note': str(data.get('note', ''))[:300],
    }
    if 'bets' in data:
        if actual:
            raise ValueError('区分別の結果は「結果取得・学習」で取得してください。')
        row['bets'] = normalize_category_bets(data['bets'])
        row['performance_version'] = 2
        apply_category_accounting(row)
    return row


def store_prediction(rows, row, include_stats=True):
    # 同じ日付・場・レースの重複保存を防止
    same_race = [
        r for r in rows
        if str(r.get('date', '')) == str(row.get('date', ''))
        and str(r.get('stadium', '')) == str(row.get('stadium', ''))
        and int(r.get('race', 0)) == int(row.get('race', 0))
    ]

    if same_race:
        if any(r.get('prediction_origin') == 'automatic' for r in same_race):
            return ({'ok': False, 'code': 'prediction_frozen',
                            'error': '展示後の自動予想を保存済みです。実績用の買い目は変更できません。'}), 409
        # 未学習の記録があれば、それを更新
        unlearned = [
            r for r in same_race
            if not r.get('learned', False)
        ]

        if unlearned:
            existing = unlearned[-1]
            existing.update(row)

            return ({
                'ok': True,
                'record': existing,
                'duplicate': True
            }), 200

        # 過去の記録がすべて学習済みなら、
        # 新しい予想として追加保存する
        rows.append(row)

        return ({
            'ok': True,
            'record': row,
            'duplicate': False
        }), 200
    rows.append(row)
    payload = {'ok': True, 'record': row}
    if include_stats:
        payload['stats'] = ledger_stats(rows)
    return payload, 200


@app.post('/api/performance')
@state_atomic
def api_performance_add():
    try:
        row = performance_record(request.get_json(force=True))
        rows = load_ledger()
        payload, status = store_prediction(rows, row)
        if payload['ok']:
            save_ledger(rows)
        return jsonify(payload), status
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

@app.delete('/api/performance')
@state_atomic
def api_performance_delete():
    rows = load_ledger()
    save_ledger([])
    return jsonify({'ok':True,'stats':ledger_stats([])})

@app.get('/api/model')
def api_model():
    return jsonify({'ok':True,'model':load_model()})

@app.post('/api/learn')
@state_atomic
def api_learn():
    data = request.get_json(force=True)
    predicted = [int(x) for x in data.get('predicted_order',[])][:6]
    actual = int(data.get('actual_first'))
    if not predicted or actual not in range(1,7):
        return jsonify({'ok':False,'error':'äºæ¸¬é ä½ã¨å®éã®1çèãæå®ãã¦ãã ãã'}),400
    s, hit = learn_from_record(load_model(), predicted, actual)
    return jsonify({'ok':True,'hit':bool(hit),'model':s})

def get_result_page(url):
    # 結果取得は共通GETの5秒制限を使わず、一時的な通信障害だけ再試行する。
    for attempt in range(2):
        try:
            return get(url, timeout=15)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 1:
                raise


@lru_cache(maxsize=96)
def cached_official_results(date, jcd, minute):
    # 一括取得中は同じ場の12レースを1枚の結果一覧から精算する。
    # 未確定のレースがある日は、1分ごとに一覧を取り直す。
    url = f'{BASE}resultlist?hd={date}&jcd={jcd}'
    html = unicodedata.normalize('NFKC', get_result_page(url))
    return parse_resultlist(html)


def fetch_official_result(date, jcd, race, results=None):
    list_source = f'{BASE}resultlist?hd={date}&jcd={jcd}'
    try:
        if results is None:
            results = cached_official_results(date, jcd, int(time.time() // 60))
        for result in results:
            if result['race'] == race and (result.get('payout') or 0) > 0:
                return dict(result), list_source, None
    except requests.RequestException:
        # 一覧が取得できない場合は、指定レースの結果ページを使う。
        pass

    source = f'{BASE}raceresult?hd={date}&jcd={jcd}&rno={race}'
    soup = BeautifulSoup(get_result_page(source), 'html.parser')
    text = unicodedata.normalize('NFKC', soup.get_text(' ', strip=True))
    for dash in ('−', '―', 'ー'):
        text = text.replace(dash, '-')
    pattern = r'3連単\s*([1-6])\s*-\s*([1-6])\s*-\s*([1-6])'
    match = re.search(pattern, text)
    if not match:
        return None, source, 'result_pending'
    combo = ''.join(match.groups())
    if len(set(combo)) != 3:
        raise ValueError('取得した3連単結果が不正です。')
    payout_match = re.search(pattern + r'\s*¥\s*([0-9][0-9,]*)', text)
    payout = int(payout_match.group(4).replace(',', '')) if payout_match else 0
    if payout <= 0:
        return None, source, 'payout_missing'
    return {'race': race, 'combo': combo, 'payout': payout}, source, None


@app.post('/api/settle_prediction')
def api_settle_prediction():
    data = request.get_json(force=True)
    date = normalize_date(data.get('date'))
    jcd = str(data.get('stadium', '15')).zfill(2)
    try:
        datetime.strptime(date, '%Y%m%d')
        race = int(data.get('race', 1))
        if jcd not in STADIUMS or race not in range(1, 13):
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'code': 'invalid_race',
                        'error': '日付・開催場・レース番号を確認してください。'}), 400
    payload, status = settle_saved_prediction(date, jcd, race)
    return jsonify(payload), status


def already_settled(row, race):
    return {'ok': True, 'status': 'already_settled', 'record': row,
            'model': load_model(), 'source': 'saved',
            'result': {'race': race, 'combo': row.get('actual_combo', ''),
                       'payout': row.get('official_payout', 0)},
            'learning_hit': bool(row.get('learn_hit'))}, 200


def settle_saved_prediction(date, jcd, race, official_result=None):
    result_source = f'{BASE}raceresult?hd={date}&jcd={jcd}&rno={race}'
    try:
        candidates = [row for row in load_ledger() if race_key(row) == (date, jcd, race)]
        if not candidates:
            return {'ok': False, 'code': 'prediction_missing', 'source': result_source,
                    'error': '保存済みの予想がありません。同じ日付・開催場で先にAI予想を保存してください。'}, 404
        if candidates[-1].get('learned'):
            return already_settled(candidates[-1], race)
        if official_result is None:
            result, result_source, error_code = fetch_official_result(date, jcd, race)
            if result is None:
                pending = error_code == 'result_pending'
                return {'ok': False, 'code': error_code, 'source': result_source,
                        'error': ('公式結果がまだ確定していません。時間をおいて再実行してください。'
                                  if pending else '指定レースの3連単払戻金を取得できませんでした。')}, 404 if pending else 502
        else:
            result = official_result
        return apply_official_result(date, jcd, race, result, result_source)
    except requests.RequestException:
        return {'ok': False, 'code': 'official_unavailable', 'source': result_source,
                'error': '公式サイトとの通信に失敗しました。時間をおいて再実行してください。'}, 502
    except Exception:
        app.logger.exception('Settlement failed date=%s stadium=%s race=%s', date, jcd, race)
        return {'ok': False, 'code': 'settlement_failed', 'source': result_source,
                'error': '結果の保存に失敗しました。記録を確定せず、次回再確認します。'}, 502


@state_atomic
def apply_official_result(date, jcd, race, result, result_source):
    # Reload under the shared PostgreSQL lock, after external communication.
    rows = load_ledger()
    candidates = [row for row in rows if race_key(row) == (date, jcd, race)]
    if not candidates:
        return {'ok': False, 'code': 'prediction_missing', 'error': '保存済みの予想がありません。'}, 404
    row = candidates[-1]
    if row.get('learned'):
        return already_settled(row, race)
    state, hit = settle_record(row, result, load_model())
    save_ledger(rows)
    return {'ok': True, 'status': 'settled', 'result': result, 'record': row,
            'learning_hit': bool(hit), 'model': state, 'source': result_source}, 200


def settle_record(row, result, state, persist=True):
    official_payout = result['payout']
    # 実際の結果を保存
    actual_combo = result['combo']
    row['actual_combo'] = actual_combo

    row['official_payout'] = official_payout
    row['settled'] = True

    if row.get('performance_version') == 2:
        apply_category_accounting(row)
    else:
        # 区分情報のない既存記録は、保存時の金額・買い目で精算する。
        row['hit'] = bool(row.get('combo') == actual_combo)
        investment = float(row.get('investment', 0) or 0)
        row['payout'] = (
            round(official_payout * (investment / 100.0), 2)
            if row['hit'] and investment > 0 else 0
        )
        row['profit'] = row['payout'] - investment

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
            state,
            predicted_features,
            actual_first,
            predicted_first,
            actual_features,
            persist=persist,
        )

    else:
        predicted_order = [
            int(x)
            for x in str(row.get('combo', ''))
            if x.isdigit()
        ]

        state, hit = learn_from_record(
            state,
            predicted_order,
            actual_first,
            persist=persist,
        )

    row['learned'] = True
    row['learn_hit'] = bool(hit)

    row['result_confirmed_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return state, hit

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

@app.get('/api/odds_debug')
def api_odds_debug():
    """3連単オッズの120通り対応を検証するためのデバッグAPI。"""
    date = request.args.get(
        'date',
        datetime.now().strftime('%Y%m%d')
    ).replace('/', '').replace('-', '')

    jcd = request.args.get('stadium', '15')
    race = int(request.args.get('race', '1'))

    odds_source = (
        f'{BASE}odds3t?hd={date}'
        f'&jcd={jcd}'
        f'&rno={race:02d}'
    )

    try:
        html = get(odds_source)

        odds = parse_odds(html)

        checks = {}

        for key in [
            '123',
            '132',
            '213',
            '231',
            '312',
            '321',
            '364',
            '362',
            '346',
            '634',
            '635',
            '653'
        ]:
            checks[key] = odds.get(key)

        expected_keys = {
            f'{a}{b}{c}'
            for a in range(1, 7)
            for b in range(1, 7)
            for c in range(1, 7)
            if len({a, b, c}) == 3
        }

        return jsonify({
            'ok': True,
            'date': date,
            'stadium': jcd,
            'venue': STADIUMS.get(jcd, jcd),
            'race': race,

            # 120通りあるか
            'odds_count': len(odds),

            # 実際に数値が入っている件数
            'numeric_count': sum(
                v is not None
                for v in odds.values()
            ),

            # 重複キーがないか
            'key_count': len(set(odds.keys())),

            # 120通りの組み合わせが完全に揃っているか
            'all_keys_valid': (
                set(odds.keys()) == expected_keys
            ),

            # 代表的な組み合わせのオッズ
            'sample_odds': checks,

            # 取得元
            'odds_source': odds_source
        })

    except Exception as e:
        return jsonify({
            'ok': False,
            'error': (
                f'{type(e).__name__}: {e}'
            ),
            'odds_source': odds_source
        }), 502
@app.get('/api/odds_debug_all')
def api_odds_debug_all():
    """開催中の全場をバックグラウンドで検証する。"""

    import threading
    import uuid
    from concurrent.futures import ThreadPoolExecutor, as_completed

    date = request.args.get(
        'date',
        datetime.now().strftime('%Y%m%d')
    ).replace('/', '').replace('-', '')

    expected_keys = {
        f'{a}{b}{c}'
        for a in range(1, 7)
        for b in range(1, 7)
        for c in range(1, 7)
        if len({a, b, c}) == 3
    }

    # ジョブ保存領域
    if not hasattr(api_odds_debug_all, 'jobs'):
        api_odds_debug_all.jobs = {}
        api_odds_debug_all.lock = threading.Lock()

    job_id = uuid.uuid4().hex[:12]

    # 開催場を取得
    try:
        active = get_active_stadiums(date)

    except Exception as e:
        return jsonify({
            'ok': False,
            'date': date,
            'error': (
                '開催場の取得に失敗しました: '
                f'{type(e).__name__}: {e}'
            )
        }), 502

    stadium_codes = [
        item['stadium']
        for item in active
    ]

    # 初期状態を保存
    with api_odds_debug_all.lock:
        api_odds_debug_all.jobs[job_id] = {
            'status': 'running',
            'date': date,
            'active_venues': active,
            'results': [],
            'completed': 0,
            'total': len(stadium_codes),
            'success_count': 0,
            'error_count': 0
        }

    def run_check():

        def check_stadium(jcd):

            race = 1

            odds_source = (
                f'{BASE}odds3t?hd={date}'
                f'&jcd={jcd}'
                f'&rno={race:02d}'
            )

            try:
                response = requests.get(
                    odds_source,
                    headers=HEAD,
                    timeout=15
                )

                response.raise_for_status()

                response.encoding = (
                    response.apparent_encoding or 'utf-8'
                )

                odds = parse_odds(response.text)

                numeric_count = sum(
                    v is not None
                    for v in odds.values()
                )

                valid = (
                    len(odds) == 120
                    and numeric_count == 120
                    and set(odds.keys()) == expected_keys
                )

                return {
                    'stadium': jcd,
                    'venue': STADIUMS.get(jcd, jcd),
                    'race': race,
                    'ok': valid,
                    'odds_count': len(odds),
                    'numeric_count': numeric_count,
                    'all_keys_valid': (
                        set(odds.keys()) == expected_keys
                    ),
                    'odds_source': odds_source
                }

            except Exception as e:

                return {
                    'stadium': jcd,
                    'venue': STADIUMS.get(jcd, jcd),
                    'race': race,
                    'ok': False,
                    'error': (
                        f'{type(e).__name__}: {e}'
                    ),
                    'odds_source': odds_source
                }

        results = []

        # 1場ずつ順番に取得
        with ThreadPoolExecutor(
            max_workers=1
        ) as executor:

            futures = {
                executor.submit(
                    check_stadium,
                    jcd
                ): jcd
                for jcd in stadium_codes
            }

            for future in as_completed(futures):

                result = future.result()
                results.append(result)

                with api_odds_debug_all.lock:

                    job = api_odds_debug_all.jobs[job_id]

                    job['results'] = sorted(
                        results,
                        key=lambda x: x['stadium']
                    )

                    job['completed'] = len(results)

                    job['success_count'] = sum(
                        1
                        for x in results
                        if x.get('ok')
                    )

                    job['error_count'] = (
                        len(results)
                        - job['success_count']
                    )

        with api_odds_debug_all.lock:

            job = api_odds_debug_all.jobs[job_id]

            job['status'] = 'completed'

    # バックグラウンド開始
    thread = threading.Thread(
        target=run_check,
        daemon=True
    )

    thread.start()

    return jsonify({
        'ok': True,
        'message': 'オッズ検証を開始しました。',
        'job_id': job_id,
        'date': date,
        'venue_count': len(stadium_codes),
        'status_url': (
            f'/api/odds_debug_all_status'
            f'?job_id={job_id}'
        )
    })


@app.get('/api/odds_debug_all_status')
def api_odds_debug_all_status():
    """バックグラウンドで実行中のオッズ検証結果を取得する。"""

    job_id = request.args.get('job_id')

    if not job_id:
        return jsonify({
            'ok': False,
            'error': 'job_idが指定されていません。'
        }), 400

    if not hasattr(api_odds_debug_all, 'jobs'):
        return jsonify({
            'ok': False,
            'error': 'ジョブが存在しません。'
        }), 404

    with api_odds_debug_all.lock:

        job = api_odds_debug_all.jobs.get(job_id)

        if job is None:
            return jsonify({
                'ok': False,
                'error': '指定されたjob_idが見つかりません。'
            }), 404

        return jsonify({
            'ok': True,
            'job_id': job_id,
            'status': job['status'],
            'date': job['date'],
            'completed': job['completed'],
            'total': job['total'],
            'success_count': job['success_count'],
            'error_count': job['error_count'],
            'active_venues': job['active_venues'],
            'results': job['results']
        })
@app.get('/api/analyze')
def api_analyze():
    date = normalize_date(request.args.get('date'))
    jcd = str(request.args.get('stadium', '15')).zfill(2)
    try:
        race = int(request.args.get('race', '9'))
        prediction = predict_race(date, jcd, race, request.args.get('fixed', 'none'),
                                  int(request.args.get('history_days', '30')))
        return jsonify(prediction)
    except Exception as error:
        return jsonify({'ok': False, 'error': str(error)}), 502


def predict_race(date, jcd, race, fixed='none', days=3, before=None, model=None, hist=None):
    source = f'{BASE}racelist?hd={date}&jcd={jcd}&rno={race:02d}'
    before_source = f'{BASE}beforeinfo?hd={date}&jcd={jcd}&rno={race:02d}'
    odds_source = f'{BASE}odds3t?hd={date}&jcd={jcd}&rno={race:02d}'
    if jcd not in STADIUMS or race not in range(1, 13):
        raise ValueError('開催場・レース番号を確認してください。')
    # 予想対象レースより未来の結果を使わない
    target_date = datetime.strptime(
        date,
        '%Y%m%d'
    ).date()

    history_end = (
        target_date - timedelta(days=1)
    ).strftime('%Y%m%d')

    urls = [source, odds_source] + ([before_source] if before is None else [])
    pages = {url: _official_pool.submit(get, url, timeout=15) for url in urls}
    # Fetch independent inputs concurrently; retain the same scoring logic.
    try:
        if hist is None:
            hist = historical_stats(jcd, days, history_end)
        if model is None:
            model = load_model()
        if before is None:
            before = parse_before(pages[before_source].result())
        entries = boats_from(pages[source].result())
        if set(entries) != set(range(1, 7)):
            raise ValueError('6艇の出走表がそろっていません。次回再確認してください。')
        boats = analyze(entries, fixed, before, hist, model=model)
        odds = parse_odds(pages[odds_source].result())
    finally:
        for page in pages.values():
            page.cancel()

    race_scenario = scenario(
        boats,
        before
    )

    combos = build_bets(
        boats,
        odds,
        fixed,
        race_scenario,
        model=model,
    )

    boats.sort(
        key=lambda x:x['score'],
        reverse=True
    )
    return {
        'ok':True,'venue':STADIUMS.get(jcd,jcd),'boats':boats,
        'main':boats[0]['boat'],'second':boats[1]['boat'],'hole':boats[2]['boat'],
        'scenario':scenario(boats,before),'bets':combos,
        'history':hist,
        'features':feature_snapshot(boats),
        'weather':{'wind':before['wind'],'wave':before['wave'],'air':before['air'],'water':before['water']},
        'odds_count':sum(v is not None for v in odds.values()),
        'notice':f'公式出走表・展示・3連単オッズと、前日までの{hist["races"]}レースを使った予想です。',
        'source':source,'before_source':before_source,'odds_source':odds_source
    }

def save_automatic_prediction(date, jcd, race, prediction, deadline, detected_at, clock=None):
    clock = clock or (lambda: datetime.now(timezone(timedelta(hours=9))))
    with atomic_state():
        if clock() >= deadline - timedelta(seconds=30):
            return None
        rows = load_ledger()
        existing = [row for row in rows if race_key(row) == (date, jcd, race)]
        if any(row.get('prediction_origin') == 'automatic' or row.get('settled') for row in existing):
            return None
        bets = normalize_category_bets(prediction.get('bets') or [])
        saved_at = clock()
        if saved_at >= deadline - timedelta(seconds=30):
            return None
        row = {
            'id': f'auto:{date}:{jcd}:{race:02d}',
            'date': date, 'stadium': jcd, 'race': race,
            'combo': str(bets[0]['bet']).replace('-', '') if bets else '',
            'predicted_first': prediction['main'], 'features': prediction['features'],
            'bets': bets, 'performance_version': 2,
            'prediction_origin': 'automatic',
            'prediction_saved_at': saved_at.isoformat(timespec='seconds'),
            'exhibition_detected_at': detected_at.isoformat(timespec='seconds'),
            'scheduled_deadline': deadline.isoformat(timespec='seconds'),
            'prediction': prediction, 'actual_combo': '', 'settled': False,
            'learned': False, 'note': '展示後の自動AI予想・各買い目100円',
        }
        apply_category_accounting(row)
        rows.append(row)
        save_ledger(rows)
        return row


def race_key(row):
    return (normalize_date(row.get('date')), str(row.get('stadium', '')).zfill(2),
            int(row.get('race', 0) or 0))

from automation import start_automation, read_status


@app.get('/api/automation')
def api_automation():
    response = jsonify(read_status())
    response.headers['Cache-Control'] = 'no-store'
    return response


from batch_processing import register_batch_routes
register_batch_routes(sys.modules[__name__])

from historical import register_historical, public_status as historical_status
register_historical(sys.modules[__name__])

start_automation(sys.modules[__name__])


if __name__ == '__main__':
    app.run(host='0.0.0.0',port=8000)


