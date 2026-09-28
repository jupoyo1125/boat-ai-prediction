import json, os
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


def load():
    if not os.path.exists(PATH):
        return json.loads(json.dumps(DEFAULT))
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        weights = DEFAULT["weights"].copy()
        weights.update(state.get("weights", {}))
        state["weights"] = weights
        state["temperature"] = max(
            8.0, min(24.0, float(state.get("temperature", DEFAULT["temperature"])))
        )
        state["samples"] = int(state.get("samples", 0))
        state["hits"] = int(state.get("hits", 0))
        return state
    except Exception:
        return json.loads(json.dumps(DEFAULT))


def save(state):
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def normalize_weights(weights):
    weights = {k: max(0.01, float(v)) for k, v in weights.items()}
    total = sum(weights.values()) or 1.0
    return {k: round(v / total, 4) for k, v in weights.items()}


def learn_from_features(state, predicted_features, actual_first, predicted_first):
    """
    Online learning.
    predicted_features:
      {"nation": 0-100, "local": 0-100, ...}
    Actual result is used only to determine whether the predicted first boat hit.
    This is a lightweight heuristic learner, not a neural network.
    """
    hit = int(predicted_first == actual_first)
    state["samples"] += 1
    state["hits"] += hit

    delta = 0.010 if hit else -0.006

    for key in state["weights"]:
        value = float(predicted_features.get(key, 50.0))
        centered = (value - 50.0) / 50.0
        state["weights"][key] += delta * centered

    state["weights"] = normalize_weights(state["weights"])

    # Calibration:
    # hit -> slightly sharper distribution, miss -> slightly flatter distribution.
    temperature = float(state.get("temperature", 12.0))
    temperature *= 0.995 if hit else 1.005
    state["temperature"] = round(max(8.0, min(24.0, temperature)), 4)

    save(state)
    return state, bool(hit)


def learn_from_record(state, predicted, actual):
    """
    Backward-compatible learning endpoint.
    `predicted` is an ordered list of boat numbers.
    """
    predicted = [int(x) for x in predicted]
    actual = int(actual)
    hit = int(bool(predicted) and predicted[0] == actual)

    state["samples"] += 1
    state["hits"] += hit

    temperature = float(state.get("temperature", 12.0))
    temperature *= 0.995 if hit else 1.005
    state["temperature"] = round(max(8.0, min(24.0, temperature)), 4)

    save(state)
    return state, bool(hit)
