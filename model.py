import json
import os
import copy
import math
from prediction_features import FEATURE_SCHEMA, FEATURE_KEYS
from datetime import datetime
from state_store import (
    state_connection, in_state_transaction, atomic_write_json, storage_required,
    database_url, storage_paused, require_storage_writable,
)

PATH = os.path.join(os.path.dirname(__file__), "model_state.json")

LEGACY_DEFAULT = {
    "weights": {
        "nation": 0.28,
        "local": 0.11,
        "motor": 0.17,
        "st": 0.18,
        "exhibition": 0.08,
        "exhibition_st": 0.03,
        "history": 0.15,
    },
    "temperature": 12.0,
    "samples": 0,
    "hits": 0,
    "updated_at": None,
}


WEIGHT_BASELINE_VERSION = '20261017-15-30-5-50'

DEFAULT = {
    'feature_schema': FEATURE_SCHEMA,
    'weight_baseline_version': WEIGHT_BASELINE_VERSION,
    'weights': {'motor': 0.15, 'exhibition': 0.30, 'meeting': 0.05, 'course': 0.50},
    'temperature': 12.0, 'samples': 0, 'hits': 0,
    'feature_samples': 0, 'feature_hits': 0, 'updated_at': None,
}


def feature_schema(state):
    return state.get('feature_schema') or (
        FEATURE_SCHEMA if set(state.get('weights', {})) == set(FEATURE_KEYS) else 'legacy-v1')


def upgrade_state(state):
    if feature_schema(state) == FEATURE_SCHEMA:
        if state.get('weight_baseline_version') == WEIGHT_BASELINE_VERSION:
            return state
        updated = copy.deepcopy(state)
        updated.setdefault('weight_baseline_history', []).append({
            'version': state.get('weight_baseline_version'),
            'weights': copy.deepcopy(state.get('weights', {})),
            'updated_at': state.get('updated_at'),
        })
        updated['weights'] = DEFAULT['weights'].copy()
        updated['weight_baseline_version'] = WEIGHT_BASELINE_VERSION
        return updated
    updated = copy.deepcopy(DEFAULT)
    # The old evidence belongs to a different feature definition. Keep it for
    # audit and keep lifetime counters, but start four-feature learning afresh.
    updated.update(samples=state.get('samples', 0), hits=state.get('hits', 0),
                   legacy_model=copy.deepcopy(state))
    return updated


def _database_url():
    return database_url()


def _db_enabled():
    return bool(_database_url())


def _db_connect():
    return state_connection(_database_url())


def _ensure_table():
    if not _db_enabled() or storage_paused():
        return

    with _db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_model_state (
                id INTEGER PRIMARY KEY,
                state JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.commit()


def _default_state():
    return json.loads(json.dumps(DEFAULT))


def _normalize_state(state):
    basis = DEFAULT if feature_schema(state) == FEATURE_SCHEMA else LEGACY_DEFAULT
    weights = basis["weights"].copy()
    weights.update({k: v for k, v in state.get("weights", {}).items() if k in weights})

    state["weights"] = weights
    state["temperature"] = max(
        8.0,
        min(18.0, float(state.get("temperature", 12.0)))
    )
    state["samples"] = int(state.get("samples", 0))
    state["hits"] = int(state.get("hits", 0))

    return state


def load():
    if _db_enabled():
        try:
            _ensure_table()

            with _db_connect() as conn:
                row = conn.execute(
                    "SELECT state FROM ai_model_state WHERE id = 1"
                ).fetchone()

            if row:
                state = row[0]

                if isinstance(state, str):
                    state = json.loads(state)

                return upgrade_state(_normalize_state(state))

            state = _default_state()
            save(state)

            return state

        except Exception:
            if in_state_transaction() or storage_required():
                raise
            pass

    if not os.path.exists(PATH):
        return _default_state()

    try:
        with open(PATH, "r", encoding="utf-8") as f:
            return upgrade_state(_normalize_state(json.load(f)))

    except Exception:
        return _default_state()


def save(state):
    require_storage_writable()
    state = _normalize_state(state)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")

    if _db_enabled():
        try:
            _ensure_table()

            with _db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO ai_model_state
                    (id, state, updated_at)
                    VALUES (1, %s::jsonb, NOW())

                    ON CONFLICT (id)
                    DO UPDATE SET
                        state = EXCLUDED.state,
                        updated_at = NOW()
                    """,
                    (
                        json.dumps(
                            state,
                            ensure_ascii=False
                        ),
                    ),
                )

                conn.commit()

            return

        except Exception:
            if in_state_transaction() or storage_required():
                raise
            pass

    atomic_write_json(PATH, state)


def normalize_weights(weights):
    weights = {
        k: max(0.01, float(v))
        for k, v in weights.items()
    }

    total = sum(weights.values()) or 1.0

    return {
        k: round(v / total, 4)
        for k, v in weights.items()
    }


def learn_from_features(
    state,
    predicted_features,
    actual_first,
    predicted_first,
    actual_features=None,
    persist=True,
):
    """
    AI予想と実際の1着艇を比較して学習する。

    predicted_features:
        AIが本命と判断した艇の特徴

    actual_features:
        実際に1着だった艇の特徴

    actual_featuresが渡されない場合は、
    従来方式の学習を行う。
    """

    hit = int(predicted_first == actual_first)

    state["samples"] += 1
    state["hits"] += hit

    if feature_schema(state) == FEATURE_SCHEMA:
        def compatible(values):
            return isinstance(values, dict) and all(
                isinstance(values.get(key), (int, float))
                and math.isfinite(values[key]) and 0 <= values[key] <= 100
                for key in FEATURE_KEYS)
        if not compatible(predicted_features) or not compatible(actual_features):
            state['legacy_settlements'] = int(state.get('legacy_settlements', 0)) + 1
            finish_learning(state, persist)
            return state, bool(hit)
        state['feature_samples'] = int(state.get('feature_samples', 0)) + 1
        state['feature_hits'] = int(state.get('feature_hits', 0)) + hit

    weights = state.get("weights", {})

    # --------------------------------------------------
    # 新しい比較学習
    # --------------------------------------------------
    if (
        isinstance(predicted_features, dict)
        and isinstance(actual_features, dict)
        and actual_features
    ):
        samples = int(state.get("feature_samples", state.get("samples", 1)))
        learning_rate = max(
            0.0025,
            0.008 * (100.0 / (100.0 + samples)) ** 0.5
        )

        for key in weights:
            predicted_value = float(
                predicted_features.get(key, 50.0)
            )

            actual_value = float(
                actual_features.get(key, 50.0)
            )

            # 実際の1着艇の方が高ければ、
            # その特徴を少し重視する。
            difference = (
                actual_value - predicted_value
            ) / 100.0

            weights[key] += (
                learning_rate * difference
            )

        state["weights"] = normalize_weights(
            weights
        )

    # --------------------------------------------------
    # 従来方式
    # actual_featuresがまだない場合の互換処理
    # --------------------------------------------------
    else:
        delta = 0.010 if hit else -0.006

        for key in weights:
            value = float(
                predicted_features.get(key, 50.0)
            )

            centered = (
                value - 50.0
            ) / 50.0

            weights[key] += (
                delta * centered
            )

        state["weights"] = normalize_weights(
            weights
        )

    # --------------------------------------------------
    # 温度パラメータも学習
    # --------------------------------------------------
    temperature = float(
        state.get("temperature", 12.0)
    )

    temperature *= (
        0.995 if hit else 1.005
    )

    state["temperature"] = round(
        max(
            8.0,
            min(24.0, temperature)
        ),
        4
    )

    finish_learning(state, persist)

    return state, bool(hit)

def learn_from_record(
    state,
    predicted,
    actual,
    persist=True,
):
    predicted = [
        int(x)
        for x in predicted
    ]

    actual = int(actual)

    hit = int(
        bool(predicted)
        and predicted[0] == actual
    )

    state["samples"] += 1
    state["hits"] += hit
    if feature_schema(state) == FEATURE_SCHEMA:
        state['legacy_settlements'] = int(state.get('legacy_settlements', 0)) + 1
        finish_learning(state, persist)
        return state, bool(hit)

    temperature = float(
    state.get("temperature", 12.0)
)

    samples = max(int(state.get("samples", 1)), 1)
    hits = max(int(state.get("hits", 0)), 0)

    hit_rate = hits / samples
    baseline = 1.0 / 6.0

    target = 12.0 - ((hit_rate - baseline) * 20.0)
    target = max(9.0, min(15.0, target))

    temperature += (target - temperature) * 0.02

    state["temperature"] = round(
        max(8.0, min(18.0, temperature)),
        4
    )
    finish_learning(state, persist)

    return state, bool(hit)


def finish_learning(state, persist):
    if persist:
        save(state)
    else:
        # Match each single-race update, including normalization, without I/O.
        _normalize_state(state)
        state['updated_at'] = datetime.now().isoformat(timespec='seconds')

