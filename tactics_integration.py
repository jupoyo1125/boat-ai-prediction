"""Bounded, auditable tactical adjustments to the existing four-feature AI.

This module never trains weights, redefines FEATURE_SCHEMA, writes results, or
changes ticket-selection rules. Inputs must come from prior-day official data.
"""
import math

TACTICS_VERSION = "tactics-course-adjustment-v1"
# One percentage-point of *estimated change relative to a player's existing
# baseline* contributes 0.45 score points. Max impact per finishing place is
# 3.6 points on the model's 0–100 score scale.
SCORE_POINTS_PER_RATE_PT = 0.45
MAX_RATE_DELTA = 8.0
MIN_RACER_STARTS = 18
MIN_COURSE_BASELINE_STARTS = 18
MIN_VENUE_COURSE_STARTS = 120
MIN_VENUE_ARCHIVE_DAYS = 60
MIN_NATIONAL_ARCHIVE_DAYS = 300
MIN_ELIGIBLE_BOATS = 3


def valid_mode(mode):
    return mode if mode in ("on", "shadow", "off") else "off"


def integrate_tactical_scores(boats, tactical_rows, readiness, mode="on"):
    """Apply independent, conservative finishing-place score adjustments.

    The input boats are from the v2 four-feature model. Do not change their
    four-feature 'courses' vector: historical learning will then keep the
    original feature semantics and weights. No leakage from the target day.
    """
    mode = valid_mode(mode)
    output = {"version": TACTICS_VERSION, "mode": mode, "applied": False,
              "eligible_boats": 0, "reason": "disabled" if mode == "off" else "insufficient_data"}
    if mode == "off":
        return output
    if not isinstance(readiness, dict) or (
            int(readiness.get("ready_days", 0) or 0) < MIN_NATIONAL_ARCHIVE_DAYS
            or int(readiness.get("venue_days", 0) or 0) < MIN_VENUE_ARCHIVE_DAYS):
        return output
    if not isinstance(boats, list) or len(boats) != 6 or len(tactical_rows) != 6:
        return output
    row_map = {int(row.get("boat", 0)): row for row in tactical_rows}
    if set(row_map) != set(range(1, 7)):
        return output

    # An exhibition's confirmed lane is essential: boat-number approximations
    # must never determine a monetary prediction's new score.
    if any(boat.get("entry_course") not in range(1, 7)
           or row_map[boat["boat"]].get("provisional_course")
           or boat.get("entry_course") != row_map[boat["boat"]].get("course")
           or str(boat.get("racer_id") or "") != str(row_map[boat["boat"]].get("racer_id") or "")
           for boat in boats):
        output["reason"] = "course_or_racer_unconfirmed"
        return output

    qualified = {}
    for boat in boats:
        row = row_map[boat["boat"]]
        if (int(row.get("starts") or 0) < MIN_RACER_STARTS
                or int(row.get("baseline_starts") or 0) < MIN_COURSE_BASELINE_STARTS
                or int(row.get("venue_starts") or 0) < MIN_VENUE_COURSE_STARTS
                or row.get("sample_warning")):
            continue
        deltas = {}
        for key in ("first", "second", "third"):
            rates = row.get("rates", {}).get(key, {})
            value = rates.get("delta")
            baseline = rates.get("base")
            original = boat.get("course_rates", {}).get(key)
            if not (isinstance(value, (int, float))
                    and isinstance(baseline, (int, float))
                    and isinstance(original, (int, float))
                    and all(math.isfinite(n) for n in (value, baseline, original))
                    and -MAX_RATE_DELTA - 0.01 <= value <= MAX_RATE_DELTA + 0.01
                    and abs(baseline - original) <= 0.3):
                break
            deltas[key] = round(float(value), 1)
        if len(deltas) == 3:
            qualified[boat["boat"]] = deltas

    output["eligible_boats"] = len(qualified)
    # Do not rank by an incompletely supported set of tactical deltas.
    if len(qualified) < MIN_ELIGIBLE_BOATS or not any(
            boat.get("entry_course") == 1 and boat["boat"] in qualified for boat in boats):
        return output

    output["reason"] = "eligible" if mode == "on" else "shadow_only"
    output["sample_window_days"] = int(readiness.get("total_days") or 0)
    output["venue_days"] = int(readiness.get("venue_days") or 0)
    for boat in boats:
        delta_rates = qualified.get(boat["boat"])
        if delta_rates is None:
            continue
        adjustment = {
            key: round(max(-MAX_RATE_DELTA, min(MAX_RATE_DELTA, delta_rates[key]))
                       * SCORE_POINTS_PER_RATE_PT, 2)
            for key in delta_rates
        }
        boat["tactics_adjustment"] = {
            "version": TACTICS_VERSION,
            "course": boat["entry_course"],
            "rate_delta_points": delta_rates,
            "score_adjustments": adjustment,
            "racer_starts": row_map[boat["boat"]]["starts"],
            "venue_starts": row_map[boat["boat"]]["venue_starts"],
            "status": "applied" if mode == "on" else "shadow",
        }
        if mode == "on":
            for place, target in (("first", "score"),
                                  ("second", "second_score"),
                                  ("third", "third_score")):
                boat[target] = round(float(boat[target]) + adjustment[place], 2)
    output["applied"] = mode == "on"
    return output
