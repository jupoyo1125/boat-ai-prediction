"""Bounded manual batches: network work first, then one locked state update."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from flask import jsonify, request
import requests


def batch_target(core, data, limit):
    if not isinstance(data, dict):
        raise ValueError('日付・開催場・レース番号を確認してください。')
    date = core.normalize_date(data.get('date'))
    datetime.strptime(date, '%Y%m%d')
    jcd = str(data.get('stadium', '')).zfill(2)
    races = data.get('races')
    if (jcd not in core.STADIUMS or not isinstance(races, list)
            or not 1 <= len(races) <= limit
            or any(type(race) is not int or race not in range(1, 13) for race in races)
            or len(set(races)) != len(races)):
        raise ValueError(f'1〜{limit}件のレース番号を指定してください。')
    return date, jcd, races


def failure(race, code, error, status=502):
    return {'race': race, 'ok': False, 'code': code, 'error': error, 'http_status': status}


def latest_predictions(core, rows, date, jcd):
    return {core.race_key(row)[2]: row for row in rows
            if core.race_key(row)[:2] == (date, jcd)}


def analyze_batch(core, date, jcd, races, fixed, days):
    # The model is read once and shared only for scoring, never modified here.
    state = core.load_model()
    history_end = (datetime.strptime(date, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
    hist = core.historical_stats(jcd, days, history_end)
    predictions, outcomes = {}, {}
    with ThreadPoolExecutor(max_workers=3) as workers:
        jobs = {race: workers.submit(core.predict_race, date, jcd, race,
                                    fixed=fixed, days=days, model=state, hist=hist)
                for race in races}
        for race, job in jobs.items():
            try:
                prediction = job.result()
                bets = prediction.get('bets') or []
                predictions[race] = core.performance_record({
                    'date': date, 'stadium': jcd, 'race': race,
                    'combo': str(bets[0]['bet']).replace('-', '') if bets else '',
                    'predicted_first': prediction['main'], 'features': prediction.get('features') or {},
                    'bets': bets, 'investment': 100, 'payout': 0, 'note': '一括AI分析',
                })
            except Exception:
                core.app.logger.exception('Batch analysis failed date=%s stadium=%s race=%s', date, jcd, race)
                outcomes[race] = failure(race, 'analysis_failed', 'AI分析に失敗しました。再実行してください。')
    if predictions:
        # Reload after external I/O: automatic predictions may have been frozen.
        with core.atomic_state():
            rows = core.load_ledger()
            changed = False
            for race in races:
                if race not in predictions:
                    continue
                payload, status = core.store_prediction(rows, predictions[race], include_stats=False)
                outcomes[race] = {'race': race, 'ok': payload['ok'], 'http_status': status}
                if payload['ok']:
                    outcomes[race]['status'] = 'saved'
                    changed = True
                else:
                    outcomes[race].update(code=payload['code'], error=payload['error'])
            if changed:
                core.save_ledger(rows)
    return [outcomes[race] for race in races]


def settle_batch(core, date, jcd, races):
    initial = latest_predictions(core, core.load_ledger(), date, jcd)
    needed = [race for race in races if race in initial and not initial[race].get('learned')]
    fetched = {}
    if needed:
        # Fetch the venue list once, even when the list is unavailable.
        try:
            results = core.cached_official_results(date, jcd, int(core.time.time() // 60))
        except requests.RequestException:
            results = []
        with ThreadPoolExecutor(max_workers=3) as workers:
            jobs = {race: workers.submit(core.fetch_official_result, date, jcd, race, results=results)
                    for race in needed}
            for race, job in jobs.items():
                try:
                    result, source, code = job.result()
                    if result:
                        fetched[race] = (result, source)
                    else:
                        pending = code == 'result_pending'
                        fetched[race] = failure(race, code,
                            '公式結果がまだ確定していません。' if pending else '3連単払戻金を取得できませんでした。',
                            404 if pending else 502)
                except requests.RequestException:
                    fetched[race] = failure(race, 'official_unavailable', '公式サイトとの通信に失敗しました。')
                except Exception:
                    core.app.logger.exception('Batch result failed date=%s stadium=%s race=%s', date, jcd, race)
                    fetched[race] = failure(race, 'settlement_failed', '公式結果を確認できませんでした。')

    outcomes, state, changed = [], None, False
    with core.atomic_state():
        rows = core.load_ledger()
        current = latest_predictions(core, rows, date, jcd)
        # Preserve the selected race order; model updates are always sequential.
        for race in races:
            row = current.get(race)
            if row is None:
                outcome = failure(race, 'prediction_missing', '保存済みの予想がありません。', 404)
            elif row.get('learned'):
                outcome = {'race': race, 'ok': True, 'status': 'already_settled', 'http_status': 200}
            elif isinstance(fetched.get(race), tuple):
                result, source = fetched[race]
                if state is None:
                    state = core.load_model()
                state, hit = core.settle_record(row, result, state, persist=False)
                outcome = {'race': race, 'ok': True, 'status': 'settled', 'http_status': 200,
                           'result': result, 'source': source, 'learning_hit': bool(hit)}
                changed = True
            else:
                outcome = fetched.get(race) or failure(race, 'retry_required', '予想が更新されました。再実行してください。')
            outcomes.append(outcome)
        if changed:
            core.save_model(state)
            core.save_ledger(rows)
    return outcomes


def register_batch_routes(core):
    @core.app.post('/api/analyze_batch')
    def api_analyze_batch():
        data = request.get_json(silent=True)
        try:
            date, jcd, races = batch_target(core, data, 3)
            fixed = str(data.get('fixed', 'none'))
            if fixed not in ('none', '1', '2', '3', '4', '5', '6'):
                raise ValueError('軸艇を確認してください。')
            days = max(1, min(int(data.get('history_days', 3)), 3))
        except (TypeError, ValueError) as error:
            return jsonify({'ok': False, 'code': 'invalid_batch', 'error': str(error)}), 400
        try:
            return jsonify({'ok': True, 'results': analyze_batch(core, date, jcd, races, fixed, days)})
        except Exception:
            core.app.logger.exception('Batch prediction storage failed')
            return jsonify({'ok': False, 'code': 'storage_failed',
                            'error': '予想の保存に失敗しました。再実行してください。'}), 502

    @core.app.post('/api/settle_batch')
    def api_settle_batch():
        data = request.get_json(silent=True)
        try:
            date, jcd, races = batch_target(core, data, 6)
        except (TypeError, ValueError) as error:
            return jsonify({'ok': False, 'code': 'invalid_batch', 'error': str(error)}), 400
        try:
            return jsonify({'ok': True, 'results': settle_batch(core, date, jcd, races)})
        except Exception:
            core.app.logger.exception('Batch settlement storage failed')
            # The shared PostgreSQL transaction rolled back both model and ledger.
            return jsonify({'ok': False, 'code': 'settlement_failed',
                            'error': '結果の保存に失敗しました。次回再確認します。'}), 502
