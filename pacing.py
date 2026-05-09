"""Load USD pacing chart JSON and compute expected splits + 1–5 workout ratings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CHART_PATH = BASE_DIR / "data" / "pacing_chart.json"

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
