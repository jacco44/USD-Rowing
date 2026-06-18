"""Load USD pacing chart JSON and compute expected splits + 1–5 workout ratings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CHART_PATH = BASE_DIR / "data" / "pacing_chart.json"

# Workouts longer than this are steady-state volume only (no split scoring).
STEADY_STATE_MIN_DURATION_SECONDS = 40 * 60

_chart_cache: dict[str, Any] | None = None


def load_chart(path: Path | None = None) -> dict[str, Any]:
    global _chart_cache
    p = path or DEFAULT_CHART_PATH
    if _chart_cache is not None and path is None:
        return _chart_cache
    data = json.loads(p.read_text(encoding="utf-8"))
    if path is None:
        _chart_cache = data
    return data


def _sorted_rows(chart: dict[str, Any]) -> list[dict[str, Any]]:
    rows = chart.get("rows") or []
    return sorted(
        [r for r in rows if r.get("time_2k_seconds") is not None],
        key=lambda r: float(r["time_2k_seconds"]),
    )


def interpolate_workouts_at_goal(chart: dict[str, Any], goal_2k_seconds: float) -> dict[str, float | None]:
    """
    Linear interpolation of each workout split (seconds / 500m or duration) across chart rows,
    keyed by target 2k test duration in seconds.
    """
    rows = _sorted_rows(chart)
    if not rows:
        return {}

    g = float(goal_2k_seconds)
    if g <= float(rows[0]["time_2k_seconds"]):
        return {k: rows[0]["workouts"].get(k) for k in rows[0]["workouts"]}
    if g >= float(rows[-1]["time_2k_seconds"]):
        return {k: rows[-1]["workouts"].get(k) for k in rows[-1]["workouts"]}

    lo = rows[0]
    hi = rows[-1]
    for a, b in zip(rows, rows[1:]):
        ta, tb = float(a["time_2k_seconds"]), float(b["time_2k_seconds"])
        if ta <= g <= tb:
            lo, hi = a, b
            break
    t_lo = float(lo["time_2k_seconds"])
    t_hi = float(hi["time_2k_seconds"])
    w_hi = (g - t_lo) / (t_hi - t_lo) if t_hi != t_lo else 0.0
    w_lo = 1.0 - w_hi

    result: dict[str, float | None] = {}
    keys = set(lo.get("workouts", {}).keys()) | set(hi.get("workouts", {}).keys())
    for k in keys:
        v_lo = lo.get("workouts", {}).get(k)
        v_hi = hi.get("workouts", {}).get(k)
        if v_lo is None and v_hi is None:
            result[k] = None
            continue
        if v_lo is None:
            result[k] = float(v_hi)
        elif v_hi is None:
            result[k] = float(v_lo)
        else:
            result[k] = w_lo * float(v_lo) + w_hi * float(v_hi)
    return result


def expected_split_for_workout(
    chart: dict[str, Any],
    goal_2k_seconds: float,
    workout_key: str,
) -> float | None:
    """Expected pace (seconds per 500m) for a workout type at the interpolated fitness level."""
    m = interpolate_workouts_at_goal(chart, goal_2k_seconds)
    v = m.get(workout_key)
    return float(v) if v is not None else None


def pace_score_from_delta(delta: float) -> float:
    """
    Continuous 1.00–5.00 score from split delta (actual − expected, seconds).
    Aligns with pace_rating bands; slower splits score lower within each band.
    """
    d = float(delta)
    if d <= 0:
        ad = abs(d)
        if ad <= 3.0:
            return 5.0
        return max(4.0, 5.0 - (ad - 3.0) / 5.0)
    if d <= 1.0:
        return 5.0 - d * 0.5
    if d <= 2.5:
        return 4.5 - (d - 1.0) / 1.5
    if d <= 4.5:
        return 3.5 - (d - 2.5) / 2.0
    if d <= 8.0:
        return 2.5 - (d - 4.5) / 3.5
    return max(1.0, 1.5 - (d - 8.0) / 12.0 * 0.5)


def workout_pace_score(
    split_delta_seconds: float | None,
    pace_rating: int | None = None,
) -> float:
    """Display score for a workout; prefers delta-based score when available."""
    if split_delta_seconds is not None:
        return pace_score_from_delta(split_delta_seconds)
    if pace_rating is not None:
        return float(pace_rating)
    return 3.0


def format_pace_score(score: float) -> str:
    """Format a pace score for display (always two decimal places)."""
    clamped = max(1.0, min(5.0, float(score)))
    return f"{clamped:.2f}"


def pace_rating(
    actual_split_seconds: float,
    expected_split_seconds: float,
) -> int:
    """
    Rate 1–5 from how close the user's average split is to the chart expectation.
    Negative delta means faster than target (better). Rating prioritizes being near or faster than expected.
    """
    delta = float(actual_split_seconds) - float(expected_split_seconds)
    ad = abs(delta)

    if delta <= 0:
        return 5 if ad <= 3.0 else 4
    if ad <= 1.0:
        return 5
    if ad <= 2.5:
        return 4
    if ad <= 4.5:
        return 3
    if ad <= 8.0:
        return 2
    return 1


def rating_label(n: int) -> str:
    return {
        5: "On pace",
        4: "Strong",
        3: "Solid",
        2: "Off pace",
        1: "Tough day",
    }.get(n, "—")


def format_split(seconds: float) -> str:
    """Format seconds as M:SS.d for erg-style splits."""
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds - m * 60
    whole_sec = int(s)
    tenths = int(round((s - whole_sec) * 10))
    if tenths >= 10:
        whole_sec += 1
        tenths = 0
    if whole_sec >= 60:
        m += whole_sec // 60
        whole_sec = whole_sec % 60
    return f"{m}:{whole_sec:02d}.{tenths}"


def parse_split(value: str) -> float:
    """Parse strings like '6:15', '1:47.3', '2:05' into seconds."""
    s = value.strip().replace(",", ".")
    if not s:
        raise ValueError("Split is empty")
    parts = s.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError("Invalid split format")


def parse_goal_2k(value: str) -> float:
    """Parse a 2k goal time (e.g. '6:15.0') into total seconds."""
    return parse_split(value)


def _workout_columns(chart: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Workout keys in chart column order, excluding total 2k time (shown as sticky row label)."""
    types = chart.get("workout_types") or {}
    items = [
        (k, meta)
        for k, meta in types.items()
        if k != "time_2k" and meta.get("column") is not None
    ]
    return sorted(items, key=lambda kv: int(kv[1]["column"]))


def format_workout_cell(seconds: float | None, kind: str | None = None) -> str:
    if seconds is None:
        return "—"
    return format_split(float(seconds))


def build_chart_table(chart: dict[str, Any]) -> dict[str, Any]:
    """
    Table-friendly view of the pacing chart for templates.
    Rows sorted by 2k time; first column is sticky 2k time, remaining columns scroll horizontally.
    """
    columns = [
        {
            "key": key,
            "header": meta.get("header") or key,
            "zone": meta.get("zone"),
            "spm_hint": meta.get("spm_hint"),
            "kind": meta.get("kind"),
        }
        for key, meta in _workout_columns(chart)
    ]
    table_rows: list[dict[str, Any]] = []
    for row in _sorted_rows(chart):
        t2k = float(row["time_2k_seconds"])
        workouts = row.get("workouts") or {}
        cells = []
        for col in columns:
            raw = workouts.get(col["key"])
            cells.append(
                {
                    "key": col["key"],
                    "display": format_workout_cell(raw, col.get("kind")),
                    "raw": raw,
                    "zone": col.get("zone"),
                }
            )
        table_rows.append(
            {
                "time_2k_seconds": t2k,
                "time_2k_display": format_split(t2k),
                "cells": cells,
            }
        )
    return {
        "title": chart.get("title") or "Pacing chart",
        "zone_summary": chart.get("zone_summary_row") or {},
        "columns": columns,
        "rows": table_rows,
    }


def chart_row_matches_goal(
    row_time_2k_seconds: float,
    goal_target_seconds: float,
    tolerance: float = 0.5,
) -> bool:
    return abs(float(row_time_2k_seconds) - float(goal_target_seconds)) <= tolerance


# Workout keys shown on the personal training plan (goal vs current splits).
PLAN_WORKOUT_KEYS = (
    "split_2k",
    "five_x_5min",
    "split_6k",
    "hop",
    "ten_k",
    "split_offset_plus_18",
    "split_offset_plus_21",
)


def estimate_2k_from_workout(
    chart: dict[str, Any],
    workout_key: str,
    avg_split_seconds: float,
) -> float | None:
    """Infer current 2k test time from a logged workout split and chart row."""
    rows = _sorted_rows(chart)
    if not rows:
        return None
    wk = workout_key or ""
    split = float(avg_split_seconds)
    if wk in ("split_2k", "time_2k"):
        return split * 4.0

    best_row: dict[str, Any] | None = None
    best_diff = float("inf")
    for row in rows:
        expected = (row.get("workouts") or {}).get(wk)
        if expected is None:
            continue
        diff = abs(float(expected) - split)
        if diff < best_diff:
            best_diff = diff
            best_row = row
    if best_row is None or best_diff > 12.0:
        return None
    return float(best_row["time_2k_seconds"])


def pick_current_2k_seconds(
    chart: dict[str, Any],
    profile_two_k: float | None,
    recent_workouts: list[dict[str, Any]] | None = None,
) -> tuple[float | None, str | None]:
    """
    Best estimate of the athlete's current 2k fitness.
    Returns (seconds, source_label) where source is 'profile', 'workout', or None.
    Steady-state pieces (>40 min) are excluded — they don't reflect test fitness.
    """
    if profile_two_k is not None:
        return float(profile_two_k), "profile"

    estimates: list[float] = []
    for w in recent_workouts or []:
        dur = effective_workout_duration(
            w.get("duration_seconds"),
            w.get("distance_meters"),
            w.get("avg_split_seconds"),
        )
        if is_steady_state_workout(dur):
            continue
        wk = w.get("workout_key")
        split = w.get("avg_split_seconds")
        if wk is None or split is None:
            continue
        est = estimate_2k_from_workout(chart, str(wk), float(split))
        if est is not None:
            estimates.append(est)
    if not estimates:
        return None, None
    return min(estimates), "workout"


def recommend_steady_minutes_per_week(
    current_2k_seconds: float,
    goal_2k_seconds: float,
    days_left: int | None = None,
) -> int:
    """
    Suggested weekly steady-state (zone 2) minutes from the gap between current and goal 2k.
    """
    gap = max(0.0, float(current_2k_seconds) - float(goal_2k_seconds))
    minutes = 90.0 + min(120.0, gap * 6.0)
    if days_left is not None and 0 < days_left <= 45:
        minutes *= 1.0 + (45 - days_left) / 90.0
    return int(round(minutes / 15.0) * 15.0)


def build_goal_plan(
    chart: dict[str, Any],
    goal_2k_seconds: float,
    current_2k_seconds: float | None = None,
    days_left: int | None = None,
) -> dict[str, Any]:
    """
    Personal training targets: chart splits at goal fitness vs current, plus steady volume.
    """
    steady_key = chart.get("default_steady_workout_key", "split_offset_plus_18")
    types = chart.get("workout_types") or {}
    goal_map = interpolate_workouts_at_goal(chart, goal_2k_seconds)
    current_2k = float(current_2k_seconds) if current_2k_seconds is not None else None
    current_map = interpolate_workouts_at_goal(chart, current_2k) if current_2k else {}

    keys = [k for k in PLAN_WORKOUT_KEYS if k in types]
    if steady_key not in keys and steady_key in types:
        keys.append(steady_key)

    rows: list[dict[str, Any]] = []
    for key in keys:
        meta = types.get(key) or {}
        goal_split = goal_map.get(key)
        cur_split = current_map.get(key) if current_2k else None
        gap = None
        if goal_split is not None and cur_split is not None:
            gap = float(cur_split) - float(goal_split)
        rows.append(
            {
                "key": key,
                "header": meta.get("header") or key,
                "zone": meta.get("zone"),
                "goal_split": goal_split,
                "current_split": cur_split,
                "gap_seconds": gap,
            }
        )

    steady_minutes = None
    if current_2k is not None:
        steady_minutes = recommend_steady_minutes_per_week(current_2k, goal_2k_seconds, days_left)

    gap_2k = (current_2k - float(goal_2k_seconds)) if current_2k is not None else None

    return {
        "goal_2k_seconds": float(goal_2k_seconds),
        "current_2k_seconds": current_2k,
        "gap_2k_seconds": gap_2k,
        "steady_key": steady_key,
        "steady_minutes_per_week": steady_minutes,
        "workout_rows": rows,
    }


def effective_workout_duration(
    duration_seconds: int | float | None,
    distance_meters: int | float | None = None,
    avg_split_seconds: float | None = None,
) -> int | None:
    """Known duration, or estimate from distance × split when both are logged."""
    if duration_seconds is not None and float(duration_seconds) > 0:
        return int(round(float(duration_seconds)))
    if distance_meters and avg_split_seconds and float(distance_meters) > 0:
        return int(round((float(distance_meters) / 500.0) * float(avg_split_seconds)))
    return None


def is_steady_state_workout(duration_seconds: int | float | None) -> bool:
    """True when duration exceeds 40 minutes — counts as steady volume, not scored."""
    if duration_seconds is None:
        return False
    return float(duration_seconds) > STEADY_STATE_MIN_DURATION_SECONDS


def workout_scoring_fields(
    actual_split: float,
    expected_split: float | None,
    duration_seconds: int | float | None,
    distance_meters: int | float | None = None,
) -> tuple[int | None, float | None, float | None]:
    """
    Returns (pace_rating, expected_split_seconds, split_delta_seconds).
    All None for steady-state workouts (>40 min) — those only count toward volume/streak.
    """
    effective_dur = effective_workout_duration(
        duration_seconds, distance_meters, actual_split
    )
    if is_steady_state_workout(effective_dur) or expected_split is None:
        return None, None, None
    delta = float(actual_split) - float(expected_split)
    return pace_rating(actual_split, expected_split), float(expected_split), delta
