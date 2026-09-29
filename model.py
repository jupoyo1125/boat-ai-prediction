import json
import os
from datetime import datetime

PATH = os.path.join(os.path.dirname(__file__), "model_state.json")

DEFAULT = {
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


def _database_url():
    return os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")


def _db_enabled():
    return bool(_database_url())


def _db_connect():
    import psycopg
    return psycopg.connect(_database_url())


def _ensure_table():
    if not _db_enabled():
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
    weights = DEFAULT["weights"].copy()
    weights.update(state.get("weights", {}))

    state["weights"] = weights
    state["temperature"] = max(
        8.0,
        min(24.0, float(state.get("temperature", 12.0)))
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

                return _normalize_state(state)

            state = _default_state()
            save(state)

            return state

        except Exception:
            pass

    if not os.path.exists(PATH):
        return _default_state()

    try:
        with open(PATH, "r", encoding="utf-8") as f:
            return _normalize_state(json.load(f))

    except Exception:
        return _default_state()


def save(state):
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
            pass

    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


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
    predicted_first
):
    hit = int(predicted_first == actual_first)

    state["samples"] += 1
    state["hits"] += hit

    delta = 0.010 if hit else -0.006

    for key in state["weights"]:
        value = float(
            predicted_features.get(key, 50.0)
        )

        centered = (value - 50.0) / 50.0

        state["weights"][key] += (
            delta * centered
        )

    state["weights"] = normalize_weights(
        state["weights"]
    )

    temperature = float(
        state.get("temperature", 12.0)
    )

    temperature *= (
        0.995 if hit else 1.005
    )

    state["temperature"] = round(
        max(8.0, min(24.0, temperature)),
        4
    )

    save(state)

    return state, bool(hit)


def learn_from_record(
    state,
    predicted,
    actual
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

    temperature = float(
        state.get("temperature", 12.0)
    )

    temperature *= (
        0.995 if hit else 1.005
    )

    state["temperature"] = round(
        max(8.0, min(24.0, temperature)),
        4
    )

    save(state)

    return state, bool(hit)
