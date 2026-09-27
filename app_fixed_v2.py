from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import request, jsonify
import app as base

app = base.app
BASE = base.BASE
STADIUMS = base.STADIUMS

def historical_stats_fast(jcd, days=30):
    days = max(1, min(int(days), 30))
    end = datetime.now().date()
    ds = [end - timedelta(days=i) for i in range(days)]
    first = [0] * 7
    combo = {}
    races = 0
    payouts = []
    dates = 0

    def one(d):
        try:
            url = f'{BASE}resultlist?hd={d.strftime("%Y%m%d")}&jcd={jcd}'
            return base.parse_resultlist(base.get(url))
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=min(8, days)) as ex:
        futures = [ex.submit(one, d) for d in ds]
        for fut in as_completed(futures):
            rs = fut.result()
            if not rs:
                continue
            dates += 1
            for r in rs:
                races += 1
                a, b, c = map(int, r['combo'])
                first[a] += 1
                combo[r['combo']] = combo.get(r['combo'], 0) + 1
                if r['payout'] is not None:
                    payouts.append(r['payout'])

    rates = {
        str(i): round(first[i] / races * 100, 2) if races else 0
        for i in range(1, 7)
    }
    top = sorted(combo.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        'days': days,
        'dates': dates,
        'races': races,
        'first_win_rate': rates,
        'top_combos': top,
        'avg_payout': round(sum(payouts) / len(payouts)) if payouts else None,
        'max_payout': max(payouts) if payouts else None
    }

def api_analyze_fixed():
    date = request.args.get(
        'date', datetime.now().strftime('%Y%m%d')
    ).replace('/', '').replace('-', '')
    jcd = request.args.get('stadium', '15')
    race = int(request.args.get('race', '9'))
    fixed = request.args.get('fixed', 'none')
    days = int(request.args.get('history_days', '30'))

    source = f'{BASE}racelist?hd={date}&jcd={jcd}&rno={race:02d}'
    before_source = f'{BASE}beforeinfo?hd={date}&jcd={jcd}&rno={race:02d}'
    odds_source = f'{BASE}odds3t?hd={date}&jcd={jcd}&rno={race:02d}'

    # Core race data is required. Historical data is optional so it can
    # never prevent the current race analysis from completing.
    try:
        before = base.parse_before(base.get(before_source))
    except Exception as e:
        return jsonify({
            'ok': False,
            'error': f'直前情報の取得に失敗しました: {type(e).__name__}: {e}',
            'source': source,
            'before_source': before_source,
            'odds_source': odds_source
        }), 502

    try:
        boats_raw = base.boats_from(base.get(source))
        if not boats_raw:
            raise RuntimeError('出走表を解析できませんでした')
    except Exception as e:
        return jsonify({
            'ok': False,
            'error': f'出走表の取得に失敗しました: {type(e).__name__}: {e}',
            'source': source,
            'before_source': before_source,
            'odds_source': odds_source
        }), 502

    # Use no historical correction if the optional history request fails.
    hist = {
        'days': days,
        'dates': 0,
        'races': 0,
        'first_win_rate': {str(i): 0 for i in range(1, 7)},
        'top_combos': [],
        'avg_payout': None,
        'max_payout': None
    }
    try:
        hist = historical_stats_fast(jcd, min(days, 7))
    except Exception:
        pass

    try:
        boats = base.analyze(boats_raw, fixed, before, hist)
    except Exception as e:
        return jsonify({
            'ok': False,
            'error': f'スコア計算に失敗しました: {type(e).__name__}: {e}',
            'source': source,
            'before_source': before_source,
            'odds_source': odds_source
        }), 502

    # Odds are useful but must not prevent the score/race judgement from
    # being displayed. Return an empty odds set if the official odds page
    # cannot be parsed.
    odds = {}
    odds_error = None
    try:
        odds = base.parse_odds(base.get(odds_source))
    except Exception as e:
        odds_error = f'{type(e).__name__}: {e}'

    try:
        combos = base.build_bets(boats, odds, fixed)
    except Exception:
        combos = []

    boats.sort(key=lambda x: x['score'], reverse=True)

    return jsonify({
        'ok': True,
        'venue': STADIUMS.get(jcd, jcd),
        'boats': boats,
        'main': boats[0]['boat'],
        'second': boats[1]['boat'] if len(boats) > 1 else boats[0]['boat'],
        'hole': boats[2]['boat'] if len(boats) > 2 else boats[-1]['boat'],
        'scenario': base.scenario(boats, before),
        'bets': combos[:12],
        'history': hist,
        'weather': {
            'wind': before['wind'],
            'wave': before['wave'],
            'air': before['air'],
            'water': before['water']
        },
        'odds_count': sum(v is not None for v in odds.values()),
        'odds_error': odds_error,
        'notice': (
            f'公式出走表・直前情報を優先。'
            f'直近{hist["days"]}日・{hist["races"]}レースの場別結果を補正に使用。'
        ),
        'source': source,
        'before_source': before_source,
        'odds_source': odds_source
    })

# Replace the original slow /api/analyze handler.
app.view_functions['api_analyze'] = api_analyze_fixed
