"""Serve the USD-Rowing login page with MySQL-backed authentication."""

import json
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from datetime import date as date_class  # alias used in goals_list for clarity
from functools import wraps
from pathlib import Path

import mysql.connector
from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import ocr_processor
import pacing
from database import get_db_connection


def verify_password(stored: str | None, provided: str) -> bool:
    """Accept Werkzeug password hashes or legacy plain text stored in the database."""
    if not stored or not provided:
        return False
    if check_password_hash(stored, provided):
        return True
    if len(stored) != len(provided):
        return False
    return secrets.compare_digest(stored, provided)


USD_EMAIL_SUFFIX = "@sandiego.edu"
MIN_PASSWORD_LENGTH = 8
# Typical %max-HR zone ceilings for max HR ~190 (college-aged athlete); users can edit.
DEFAULT_HR_ZONE_MAX_BPM = (114, 133, 152, 171, 190)
REGISTER_TEMPLATE_CTX = {
    "min_password_length": MIN_PASSWORD_LENGTH,
    "hr_zones_default": DEFAULT_HR_ZONE_MAX_BPM,
}


def is_valid_usd_email(email: str) -> bool:
    email = email.strip()
    return bool(email) and email.lower().endswith(USD_EMAIL_SUFFIX.lower())


def format_minutes(minutes: float) -> str:
    """Format a duration in minutes as 'Xh Ym' or 'X min'."""
    minutes = max(0.0, float(minutes))
    if minutes < 60:
        return f"{minutes:.0f} min"
    h = int(minutes // 60)
    m = int(minutes % 60)
    if m == 0:
        return f"{h}h"
    return f"{h}h {m}m"


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.jinja_env.globals["format_split"] = pacing.format_split
app.jinja_env.globals["format_minutes"] = format_minutes
app.jinja_env.globals["format_pace_score"] = pacing.format_pace_score
app.jinja_env.globals["is_steady_workout"] = pacing.is_steady_state_workout
app.jinja_env.globals["effective_workout_duration"] = pacing.effective_workout_duration

COXSWAIN_WEEKLY_TARGET = 5


@app.context_processor
def inject_admin_flag():
    return {"is_admin": is_admin()}


@app.context_processor
def inject_coxswain_access():
    return {
        "has_coxswain_access": has_coxswain_access(),
        "coxswain_weekly_target": COXSWAIN_WEEKLY_TARGET,
    }


_AUTO_OCR_INTERVAL_SECONDS = max(15, int(os.environ.get("AUTO_OCR_INTERVAL_SECONDS", "45")))


def _auto_ocr_loop() -> None:
    while True:
        time.sleep(_AUTO_OCR_INTERVAL_SECONDS)
        if os.environ.get("AUTO_OCR", "1") == "0":
            continue
        try:
            result = ocr_processor.process_all_pending()
            if result["processed"]:
                print(f"Auto OCR: processed {result['processed']} scan(s)")
        except Exception as err:  # noqa: BLE001
            print(f"Auto OCR error: {err}")


def _start_auto_ocr_poller() -> None:
    if os.environ.get("AUTO_OCR", "1") == "0":
        return
    thread = threading.Thread(target=_auto_ocr_loop, daemon=True, name="auto-ocr")
    thread.start()


# Skip Flask debug-reloader parent to avoid duplicate pollers.
_is_debug_reloader_parent = (
    os.environ.get("WERKZEUG_RUN_MAIN") is None and os.environ.get("FLASK_DEBUG") == "1"
)
if not _is_debug_reloader_parent:
    _start_auto_ocr_poller()

TRACKER_TABLES_MSG = (
    "Tracker tables are missing. Apply schema.sql to your MySQL database to enable goals and workouts."
)


def _ensure_goal_completion_column(conn) -> None:
    """Add is_completed column to erg_goals if it doesn't exist yet."""
    try:
        cur = conn.cursor()
        cur.execute(
            "ALTER TABLE erg_goals ADD COLUMN is_completed TINYINT(1) NOT NULL DEFAULT 0"
        )
        conn.commit()
        cur.close()
    except mysql.connector.Error as err:
        if getattr(err, "errno", None) != 1060:  # 1060 = Duplicate column name
            raise
        conn.rollback()


def _ensure_user_profile_columns(conn) -> None:
    """Add optional athlete profile columns to rowing_users if missing."""
    alters = (
        "ALTER TABLE rowing_users ADD COLUMN two_k_seconds INT NULL",
        "ALTER TABLE rowing_users ADD COLUMN six_k_seconds INT NULL",
        "ALTER TABLE rowing_users ADD COLUMN is_coxswain TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE rowing_users ADD COLUMN hr_zone1_max SMALLINT UNSIGNED NULL",
        "ALTER TABLE rowing_users ADD COLUMN hr_zone2_max SMALLINT UNSIGNED NULL",
        "ALTER TABLE rowing_users ADD COLUMN hr_zone3_max SMALLINT UNSIGNED NULL",
        "ALTER TABLE rowing_users ADD COLUMN hr_zone4_max SMALLINT UNSIGNED NULL",
        "ALTER TABLE rowing_users ADD COLUMN hr_zone5_max SMALLINT UNSIGNED NULL",
    )
    for stmt in alters:
        try:
            cur = conn.cursor()
            cur.execute(stmt)
            conn.commit()
            cur.close()
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) != 1060:
                raise
            conn.rollback()


def _registration_form_values() -> dict[str, str]:
    """Sticky values for the register form (defaults on GET, submitted on POST)."""
    z = DEFAULT_HR_ZONE_MAX_BPM

    def zone_field(i: int) -> str:
        key = f"hr_zone{i}_max"
        if request.method == "POST":
            return (request.form.get(key) or "").strip()
        return str(z[i - 1])

    if request.method == "POST":
        is_coxswain = (request.form.get("is_coxswain") or "").strip()
        no_erg_times = request.form.get("no_erg_times") == "1"
    else:
        is_coxswain = ""
        no_erg_times = False

    return {
        "two_k": (request.form.get("two_k", "") or "").strip() if request.method == "POST" else "",
        "six_k": (request.form.get("six_k", "") or "").strip() if request.method == "POST" else "",
        "is_coxswain": is_coxswain,
        "no_erg_times": no_erg_times,
        "hr_zone1_max": zone_field(1),
        "hr_zone2_max": zone_field(2),
        "hr_zone3_max": zone_field(3),
        "hr_zone4_max": zone_field(4),
        "hr_zone5_max": zone_field(5),
    }


def _parse_optional_erg_time(raw: str, kind: str) -> tuple[int | None, str | None]:
    """Parse an optional erg test time; return (seconds, user-facing error)."""
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        seconds = int(round(pacing.parse_erg_test_time(text)))
    except ValueError:
        example = "6:45.0" if kind == "2k" else "21:30.0"
        return None, f"Enter a valid current {kind} time (for example {example})."
    err = pacing.validate_erg_test_seconds(seconds, kind)
    if err:
        return None, err
    return seconds, None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


# Admin access: if ADMIN_EMAILS env var is set (comma-separated), only those
# accounts can reach /admin/*. If the variable is empty, any logged-in user
# can access admin (suitable for a small trusted team).
_ADMIN_EMAILS: set[str] = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
}


def is_admin() -> bool:
    user = session.get("user", "")
    return bool(user and (not _ADMIN_EMAILS or user.lower() in _ADMIN_EMAILS))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        if not is_admin():
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped


_workout_verification_columns_ready = False


def _ensure_workout_verification_columns(conn) -> None:
    """Add coxswain verification columns to erg_workouts if missing."""
    global _workout_verification_columns_ready
    if _workout_verification_columns_ready:
        return
    alters = (
        "ALTER TABLE erg_workouts ADD COLUMN verified_at DATETIME NULL",
        "ALTER TABLE erg_workouts ADD COLUMN verified_by VARCHAR(255) NULL",
    )
    for stmt in alters:
        try:
            cur = conn.cursor()
            cur.execute(stmt)
            conn.commit()
            cur.close()
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) != 1060:
                raise
            conn.rollback()
    _workout_verification_columns_ready = True


def _current_user_is_coxswain() -> bool:
    user = session.get("user")
    if not user:
        return False
    conn = get_db_connection()
    if conn is None:
        return False
    try:
        _ensure_user_profile_columns(conn)
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT is_coxswain FROM rowing_users WHERE username = %s",
            (user,),
        )
        row = cur.fetchone()
        cur.close()
        return bool(row and row.get("is_coxswain"))
    except mysql.connector.Error:
        return False
    finally:
        conn.close()


def has_coxswain_access() -> bool:
    """Coxswain accounts and admins can open the team workspace."""
    if not session.get("user"):
        return False
    return is_admin() or _current_user_is_coxswain()


def coxswain_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        if not has_coxswain_access():
            flash("Coxswain workspace access required.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped


def _week_bounds_mon_sun(ref: date) -> tuple[date, date]:
    """Monday–Sunday bounds for the week containing ref."""
    start = ref - timedelta(days=ref.weekday())
    return start, start + timedelta(days=6)


def _parse_week_param(raw: str | None, today: date) -> tuple[date, date]:
    if raw:
        try:
            ref = date.fromisoformat(raw[:10])
            return _week_bounds_mon_sun(ref)
        except ValueError:
            pass
    return _week_bounds_mon_sun(today)


def _display_name(username: str) -> str:
    return username.split("@")[0] if "@" in username else username


def _ensure_whatsapp_phone_column(conn) -> None:
    """Add whatsapp_phone to rowing_users if missing (graceful migration)."""
    try:
        cur = conn.cursor()
        cur.execute(
            "ALTER TABLE rowing_users ADD COLUMN whatsapp_phone VARCHAR(30) NULL"
        )
        conn.commit()
        cur.close()
    except mysql.connector.Error as err:
        if getattr(err, "errno", None) != 1060:  # 1060 = Duplicate column
            raise
        conn.rollback()


_streak_columns_ready = False


def _ensure_streak_columns(conn) -> None:
    """Add streak_count and streak_last_workout_date to rowing_users if missing."""
    global _streak_columns_ready
    if _streak_columns_ready:
        return
    alters = (
        "ALTER TABLE rowing_users ADD COLUMN streak_count INT NOT NULL DEFAULT 0",
        "ALTER TABLE rowing_users ADD COLUMN streak_last_workout_date DATE NULL",
    )
    for stmt in alters:
        try:
            cur = conn.cursor()
            cur.execute(stmt)
            conn.commit()
            cur.close()
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) != 1060:
                raise
            conn.rollback()
    _streak_columns_ready = True


def _update_streak(conn, username: str, workout_date_str: str) -> int:
    """Increment (or maintain) the streak after a workout on workout_date_str.

    Rules:
    - Same day as last workout   → no change.
    - 1-day gap                  → streak + 1.
    - 2-day gap (forgiveness)    → streak + 1.
    - 3+ day gap                 → reset to 1.

    Returns the resulting streak count.
    """
    _ensure_streak_columns(conn)
    try:
        workout_date = date.fromisoformat(workout_date_str)
    except (ValueError, TypeError):
        workout_date = date.today()

    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT streak_count, streak_last_workout_date "
            "FROM rowing_users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
        cur.close()
    except mysql.connector.Error:
        return 0

    if not row:
        return 0

    current_streak = int(row["streak_count"] or 0)
    last_date = row["streak_last_workout_date"]  # datetime.date or None

    if last_date is None:
        new_streak = 1
        new_date = workout_date
    elif workout_date <= last_date:
        # Same day or retroactive — no streak change
        return current_streak
    else:
        days_diff = (workout_date - last_date).days
        new_streak = current_streak + 1 if days_diff <= 2 else 1
        new_date = workout_date

    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE rowing_users "
            "SET streak_count = %s, streak_last_workout_date = %s "
            "WHERE username = %s",
            (new_streak, new_date, username),
        )
        conn.commit()
        cur.close()
    except mysql.connector.Error:
        conn.rollback()
        return current_streak

    return new_streak


def _streak_tier(count: int) -> str:
    if count == 0:
        return "zero"
    if count <= 3:
        return "low"
    if count <= 7:
        return "mid"
    if count <= 14:
        return "high"
    return "max"


@app.context_processor
def inject_streak():
    """Inject streak_count and streak_tier into every template context."""
    user = session.get("user")
    if not user:
        return {"streak_count": 0, "streak_tier": "zero"}
    conn = get_db_connection()
    if conn is None:
        return {"streak_count": 0, "streak_tier": "zero"}
    try:
        _ensure_streak_columns(conn)
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT streak_count FROM rowing_users WHERE username = %s", (user,)
        )
        row = cur.fetchone()
        cur.close()
        count = int(row["streak_count"] or 0) if row else 0
    except mysql.connector.Error:
        count = 0
    finally:
        conn.close()
    return {"streak_count": count, "streak_tier": _streak_tier(count)}


@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET" and session.get("user"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not is_valid_usd_email(username):
            flash("Please sign in with your @sandiego.edu email address.", "error")
            return render_template("login.html"), 400

        conn = get_db_connection()
        if conn is None:
            flash("Unable to reach the database. Please try again later.", "error")
            return render_template("login.html"), 503

        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT username, password FROM rowing_users WHERE LOWER(username) = LOWER(%s)",
                (username,),
            )
            row = cur.fetchone()
            cur.close()
        finally:
            conn.close()

        if not row or not verify_password(row.get("password"), password):
            flash("Invalid email or password.", "error")
            return render_template("login.html"), 401

        session["user"] = row["username"]
        flash("Welcome back.", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if not is_valid_usd_email(username):
            flash("Registration requires a @sandiego.edu email address.", "error")
            return render_template(
                "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
            ), 400

        if len(password) < MIN_PASSWORD_LENGTH:
            flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", "error")
            return render_template(
                "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
            ), 400

        if password != password_confirm:
            flash("Passwords do not match.", "error")
            return render_template(
                "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
            ), 400

        two_k_raw = (request.form.get("two_k") or "").strip()
        six_k_raw = (request.form.get("six_k") or "").strip()
        is_coxswain_raw = (request.form.get("is_coxswain") or "").strip()
        no_erg_times = request.form.get("no_erg_times") == "1"

        if is_coxswain_raw not in ("0", "1"):
            flash("Please indicate whether you are a coxswain.", "error")
            return render_template(
                "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
            ), 400
        is_coxswain = is_coxswain_raw == "1"

        two_k_seconds = None
        six_k_seconds = None
        if is_coxswain or no_erg_times:
            if two_k_raw or six_k_raw:
                flash("Leave 2k and 6k blank when you are a coxswain or don't have test times yet.", "error")
                return render_template(
                    "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
                ), 400
        else:
            if not two_k_raw or not six_k_raw:
                flash("Enter your current 2k and 6k times, or check that you don't have them yet.", "error")
                return render_template(
                    "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
                ), 400
            two_k_seconds, err = _parse_optional_erg_time(two_k_raw, "2k")
            if err:
                flash(err, "error")
                return render_template(
                    "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
                ), 400
            six_k_seconds, err = _parse_optional_erg_time(six_k_raw, "6k")
            if err:
                flash(err, "error")
                return render_template(
                    "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
                ), 400

        zone_vals: list[int] = []
        for i in range(1, 6):
            raw = (request.form.get(f"hr_zone{i}_max") or "").strip()
            if not raw:
                flash("Please enter all five heart-rate zone ceilings (bpm), or use the suggested defaults.", "error")
                return render_template(
                    "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
                ), 400
            try:
                zone_vals.append(int(raw))
            except ValueError:
                flash("Heart-rate zones must be whole numbers (beats per minute).", "error")
                return render_template(
                    "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
                ), 400

        for z in zone_vals:
            if z < 50 or z > 230:
                flash("Each zone ceiling should be between 50 and 230 bpm.", "error")
                return render_template(
                    "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
                ), 400

        if not all(zone_vals[i] < zone_vals[i + 1] for i in range(4)):
            flash("Heart-rate zones should increase from zone 1 through zone 5.", "error")
            return render_template(
                "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
            ), 400

        wa_phone_raw = (request.form.get("whatsapp_phone") or "").strip()
        wa_phone = re.sub(r"\D", "", wa_phone_raw) or None

        email_norm = username.lower()
        conn = get_db_connection()
        if conn is None:
            flash("Unable to reach the database. Please try again later.", "error")
            return render_template(
                "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
            ), 503

        cur = None
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT 1 FROM rowing_users WHERE LOWER(username) = LOWER(%s)",
                (email_norm,),
            )
            if cur.fetchone():
                flash("An account with this email already exists.", "error")
                return render_template(
                    "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
                ), 409

            _ensure_whatsapp_phone_column(conn)
            insert_profile = (
                "INSERT INTO rowing_users (username, password, two_k_seconds, six_k_seconds, "
                "is_coxswain, hr_zone1_max, hr_zone2_max, hr_zone3_max, hr_zone4_max, "
                "hr_zone5_max, whatsapp_phone) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            )
            insert_params = (
                email_norm,
                generate_password_hash(password),
                two_k_seconds,
                six_k_seconds,
                1 if is_coxswain else 0,
                zone_vals[0],
                zone_vals[1],
                zone_vals[2],
                zone_vals[3],
                zone_vals[4],
                wa_phone,
            )
            try:
                cur.execute(insert_profile, insert_params)
            except mysql.connector.Error as ins_err:
                if getattr(ins_err, "errno", None) == 1054:
                    _ensure_user_profile_columns(conn)
                    cur.execute(insert_profile, insert_params)
                else:
                    raise
            conn.commit()
            session["user"] = email_norm
            flash("Welcome! Your account is ready.", "success")
            return redirect(url_for("dashboard"))
        except mysql.connector.Error as err:
            conn.rollback()
            print(f"Registration database error: {err}")
            flash("Could not complete registration. Please try again.", "error")
            return render_template(
                "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
            ), 500
        finally:
            if cur is not None:
                cur.close()
            conn.close()

    return render_template(
        "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
    )


@login_required
@app.route("/dashboard")
def dashboard():
    user = session["user"]
    chart = pacing.load_chart()
    workout_types = chart.get("workout_types", {})
    stats = {"goals": 0, "workouts_week": 0, "steady_minutes_week": 0}
    recent_workouts = []
    primary_goal = None
    goal_plan = None
    current_2k_source = None
    predictions_enabled = False
    is_coxswain = False
    conn = get_db_connection()
    if conn is None:
        flash("Unable to reach the database.", "error")
    else:
        try:
            _ensure_user_profile_columns(conn)
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT two_k_seconds, six_k_seconds, is_coxswain FROM rowing_users WHERE username = %s",
                (user,),
            )
            profile_row = cur.fetchone()
            predictions_enabled = pacing.profile_supports_predictions(profile_row)
            is_coxswain = bool(profile_row and profile_row.get("is_coxswain"))
            profile_two_k = (
                float(profile_row["two_k_seconds"])
                if predictions_enabled and profile_row.get("two_k_seconds") is not None
                else None
            )

            cur.execute(
                "SELECT COUNT(*) AS c FROM erg_goals WHERE username = %s AND is_completed = 0",
                (user,),
            )
            stats["goals"] = cur.fetchone()["c"]
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM erg_workouts
                WHERE username = %s AND workout_date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                """,
                (user,),
            )
            stats["workouts_week"] = cur.fetchone()["c"]
            cur.execute(
                """
                SELECT COALESCE(SUM(duration_seconds), 0) / 60.0 AS steady_min
                FROM erg_workouts
                WHERE username = %s
                  AND workout_date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                  AND duration_seconds > %s
                """,
                (user, pacing.STEADY_STATE_MIN_DURATION_SECONDS),
            )
            row = cur.fetchone()
            if row and row["steady_min"] is not None:
                stats["steady_minutes_week"] = int(round(float(row["steady_min"])))

            cur.execute(
                """
                SELECT id, title, target_seconds, target_date
                FROM erg_goals
                WHERE username = %s AND is_completed = 0
                ORDER BY target_date ASC
                LIMIT 1
                """,
                (user,),
            )
            primary_goal = cur.fetchone()
            if primary_goal:
                td = primary_goal.get("target_date")
                days_left = (td - date.today()).days if td else None
                primary_goal["days_left"] = days_left

            cur.execute(
                """
                SELECT id, workout_date, avg_split_seconds, label, workout_key,
                       duration_seconds, distance_meters
                FROM erg_workouts WHERE username = %s
                ORDER BY workout_date DESC, id DESC LIMIT 12
                """,
                (user,),
            )
            recent_workouts = cur.fetchall()
            cur.close()

            if primary_goal and predictions_enabled and primary_goal.get("target_seconds") is not None:
                current_2k_source = "profile"
                goal_plan = pacing.build_goal_plan(
                    chart,
                    float(primary_goal["target_seconds"]),
                    profile_two_k,
                    primary_goal.get("days_left"),
                )
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) != 1146:
                raise
            flash(TRACKER_TABLES_MSG, "error")
        finally:
            conn.close()

    celebrate = session.pop("celebrate", None)
    return render_template(
        "dashboard.html",
        email=user,
        workout_types=workout_types,
        stats=stats,
        recent_workouts=recent_workouts,
        primary_goal=primary_goal,
        goal_plan=goal_plan,
        current_2k_source=current_2k_source,
        predictions_enabled=predictions_enabled,
        is_coxswain=is_coxswain,
        celebrate=celebrate,
        format_split=pacing.format_split,
    )


@login_required
@app.route("/goals")
def goals_list():
    user = session["user"]
    rows = []
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor(dictionary=True)
            _GOALS_QUERY = """
                SELECT id, title, target_seconds, target_date, is_public, created_at, is_completed
                FROM erg_goals WHERE username = %s ORDER BY is_completed ASC, target_date ASC
            """
            try:
                cur.execute(_GOALS_QUERY, (user,))
            except mysql.connector.Error as col_err:
                if getattr(col_err, "errno", None) == 1054:
                    _ensure_goal_completion_column(conn)
                    cur.execute(_GOALS_QUERY, (user,))
                else:
                    raise
            rows = cur.fetchall()
            cur.close()
            today = date_class.today()
            for row in rows:
                td = row.get("target_date")
                row["days_left"] = (td - today).days if td else None
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) != 1146:
                raise
            flash(TRACKER_TABLES_MSG, "error")
        finally:
            conn.close()
    chart = pacing.load_chart()
    pacing_chart = pacing.build_chart_table(chart)
    goal_targets = [
        float(g["target_seconds"])
        for g in rows
        if g.get("target_seconds") is not None and not g.get("is_completed")
    ]
    goal_plans: dict[int, dict] = {}
    predictions_enabled = False
    is_coxswain = False
    profile_two_k = None
    conn2 = get_db_connection()
    if conn2:
        try:
            _ensure_user_profile_columns(conn2)
            cur = conn2.cursor(dictionary=True)
            cur.execute(
                "SELECT two_k_seconds, six_k_seconds, is_coxswain FROM rowing_users WHERE username = %s",
                (user,),
            )
            prow = cur.fetchone()
            predictions_enabled = pacing.profile_supports_predictions(prow)
            is_coxswain = bool(prow and prow.get("is_coxswain"))
            if predictions_enabled and prow.get("two_k_seconds") is not None:
                profile_two_k = float(prow["two_k_seconds"])
            cur.close()
        except mysql.connector.Error:
            pass
        finally:
            conn2.close()

    today = date_class.today()
    if predictions_enabled and profile_two_k is not None:
        for g in rows:
            if g.get("is_completed") or g.get("target_seconds") is None:
                continue
            td = g.get("target_date")
            days_left = (td - today).days if td else None
            goal_plans[g["id"]] = pacing.build_goal_plan(
                chart, float(g["target_seconds"]), profile_two_k, days_left
            )

    return render_template(
        "goals.html",
        goals=rows,
        goal_plans=goal_plans,
        current_2k_source="profile" if predictions_enabled else None,
        predictions_enabled=predictions_enabled,
        is_coxswain=is_coxswain,
        format_split=pacing.format_split,
        today=today,
        pacing_chart=pacing_chart,
        goal_targets=goal_targets,
        chart_row_matches_goal=pacing.chart_row_matches_goal,
    )


@login_required
@app.route("/goals/new", methods=["GET", "POST"])
def goal_new():
    user = session["user"]
    if request.method == "POST":
        title = (request.form.get("title") or "").strip() or None
        raw_goal = request.form.get("target_2k", "")

        try:
            target_seconds = pacing.parse_goal_2k(raw_goal)
        except ValueError:
            flash("Enter a valid 2k goal time (for example 6:15.0).", "error")
            return render_template("goal_new.html", today_iso=date.today().isoformat()), 400

        target_date = request.form.get("target_date") or ""
        if not target_date:
            flash("Choose a target date for your goal.", "error")
            return render_template("goal_new.html", today_iso=date.today().isoformat()), 400

        is_public = 0

        conn = get_db_connection()
        if conn is None:
            flash("Database unavailable.", "error")
            return render_template("goal_new.html", today_iso=date.today().isoformat()), 503

        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO erg_goals (username, title, goal_kind, target_seconds, target_date, is_public)
                VALUES (%s, %s, 'time_2k', %s, %s, %s)
                """,
                (user, title, target_seconds, target_date, is_public),
            )
            conn.commit()
            cur.close()
        except mysql.connector.Error as err:
            conn.rollback()
            if getattr(err, "errno", None) == 1146:
                flash(TRACKER_TABLES_MSG, "error")
            else:
                print(f"Goal save error: {err}")
                flash("Could not save your goal.", "error")
            return render_template("goal_new.html", today_iso=date.today().isoformat()), 500
        finally:
            conn.close()

        session["celebrate"] = "goal_created"
        flash("Goal set! Time to get to work.", "success")
        return redirect(url_for("goals_list"))

    return render_template("goal_new.html", today_iso=date.today().isoformat())


@login_required
@app.route("/workouts")
def workouts_list():
    user = session["user"]
    rows = []
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT w.id, w.workout_date, w.label, w.avg_split_seconds, w.pace_rating,
                       w.expected_split_seconds, w.split_delta_seconds, w.workout_key,
                       w.duration_seconds, w.distance_meters,
                       g.title AS goal_title
                FROM erg_workouts w
                LEFT JOIN erg_goals g ON w.goal_id = g.id
                WHERE w.username = %s
                ORDER BY w.workout_date DESC, w.id DESC
                """,
                (user,),
            )
            rows = cur.fetchall()
            cur.close()
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) != 1146:
                raise
            flash(TRACKER_TABLES_MSG, "error")
        finally:
            conn.close()

    today = date.today()
    cal_y, cal_m, cal_first, cal_last = _calendar_month_bounds(request, today)
    cal_events = _workout_calendar_events_for_month(user, cal_first, cal_last)

    chart = pacing.load_chart()
    return render_template(
        "workouts.html",
        workouts=rows,
        workout_types=chart.get("workout_types", {}),
        format_split=pacing.format_split,
        events_json=json.dumps(cal_events),
        cal_year=cal_y,
        cal_month=cal_m,
        cal_date_iso=cal_first.isoformat(),
    )


@login_required
@app.route("/workouts/new", methods=["GET", "POST"])
def workout_new():
    user = session["user"]
    chart = pacing.load_chart()
    workout_types = chart.get("workout_types", {})
    default_key = chart.get("default_steady_workout_key", "split_offset_plus_18")

    goals = []
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT id, title, target_seconds, target_date
                FROM erg_goals WHERE username = %s ORDER BY target_date ASC
                """,
                (user,),
            )
            goals = cur.fetchall()
            cur.close()
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) != 1146:
                raise
            flash(TRACKER_TABLES_MSG, "error")
        finally:
            conn.close()

    if request.method == "POST":
        if not goals:
            flash("Create a goal first so we can compare your splits to the pacing chart.", "error")
            return redirect(url_for("goal_new"))

        goal_id = request.form.get("goal_id") or ""
        try:
            gid = int(goal_id)
        except ValueError:
            gid = 0

        try:
            actual_split = pacing.parse_split(request.form.get("avg_split", ""))
        except ValueError:
            flash("Enter your average split like 2:05.5 (pace per 500m).", "error")
            return (
                render_template(
                    "workout_new.html",
                    goals=goals,
                    workout_types=workout_types,
                    default_key=default_key,
                    today_iso=date.today().isoformat(),
                ),
                400,
            )

        wk_key = request.form.get("workout_key") or default_key
        if wk_key not in workout_types:
            wk_key = default_key

        workout_date = request.form.get("workout_date") or date.today().isoformat()
        label = (request.form.get("label") or "").strip() or None
        notes = (request.form.get("notes") or "").strip() or None

        dur_raw = (request.form.get("duration_seconds") or "").strip()
        duration_seconds = int(dur_raw) if dur_raw.isdigit() else None

        dist_raw = (request.form.get("distance_meters") or "").strip()
        distance_meters = int(dist_raw) if dist_raw.isdigit() else None

        effective_dur = pacing.effective_workout_duration(
            duration_seconds, distance_meters, actual_split
        )
        if duration_seconds is None and effective_dur is not None:
            duration_seconds = effective_dur

        conn = get_db_connection()
        if conn is None:
            flash("Database unavailable.", "error")
            return render_template(
                "workout_new.html",
                goals=goals,
                workout_types=workout_types,
                default_key=default_key,
                today_iso=date.today().isoformat(),
            ), 503

        cur = None
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, target_seconds FROM erg_goals WHERE id = %s AND username = %s",
                (gid, user),
            )
            g_row = cur.fetchone()
            if not g_row:
                flash("Pick one of your goals.", "error")
                return (
                    render_template(
                        "workout_new.html",
                        goals=goals,
                        workout_types=workout_types,
                        default_key=default_key,
                        today_iso=date.today().isoformat(),
                    ),
                    400,
                )

            is_steady = pacing.is_steady_state_workout(effective_dur)
            expected = None
            if not is_steady:
                expected = pacing.expected_split_for_workout(
                    chart, float(g_row["target_seconds"]), wk_key
                )
                if expected is None:
                    flash("Could not compute an expected split from the pacing chart.", "error")
                    return (
                        render_template(
                            "workout_new.html",
                            goals=goals,
                            workout_types=workout_types,
                            default_key=default_key,
                            today_iso=date.today().isoformat(),
                        ),
                        500,
                    )

            rating, expected, delta = pacing.workout_scoring_fields(
                actual_split, expected, duration_seconds, distance_meters
            )

            cur.execute(
                """
                INSERT INTO erg_workouts (
                    username, goal_id, workout_date, label, duration_seconds, distance_meters,
                    avg_split_seconds, workout_key, pace_rating, expected_split_seconds,
                    split_delta_seconds, notes
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    user,
                    gid,
                    workout_date,
                    label,
                    duration_seconds,
                    distance_meters,
                    actual_split,
                    wk_key,
                    rating,
                    expected,
                    delta,
                    notes,
                ),
            )
            conn.commit()
            _update_streak(conn, user, workout_date)
            if is_steady:
                flash(
                    f"Steady workout logged — {format_minutes(effective_dur / 60.0)} "
                    "toward your weekly target.",
                    "success",
                )
            else:
                if delta is not None and delta <= 0:
                    session["celebrate"] = "perfect_workout"
                flash(
                    f"Workout logged — target split {pacing.format_split(expected)} "
                    f"({delta:+.1f}s vs chart).",
                    "success",
                )
            return redirect(url_for("dashboard"))
        except mysql.connector.Error as err:
            conn.rollback()
            if getattr(err, "errno", None) == 1146:
                flash(TRACKER_TABLES_MSG, "error")
            else:
                print(f"Workout save error: {err}")
                flash("Could not save workout.", "error")
            return (
                render_template(
                    "workout_new.html",
                    goals=goals,
                    workout_types=workout_types,
                    default_key=default_key,
                    today_iso=date.today().isoformat(),
                ),
                500,
            )
        finally:
            if cur is not None:
                cur.close()
            conn.close()

    return render_template(
        "workout_new.html",
        goals=goals,
        workout_types=workout_types,
        default_key=default_key,
        today_iso=date.today().isoformat(),
    )


@login_required
@app.route("/goals/<int:goal_id>/edit", methods=["GET", "POST"])
def goal_edit(goal_id):
    user = session["user"]
    conn = get_db_connection()
    if conn is None:
        flash("Database unavailable.", "error")
        return redirect(url_for("goals_list"))

    goal = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, title, target_seconds, target_date, is_public FROM erg_goals WHERE id = %s AND username = %s",
            (goal_id, user),
        )
        goal = cur.fetchone()
        cur.close()
    except mysql.connector.Error as err:
        if getattr(err, "errno", None) != 1146:
            raise
        flash(TRACKER_TABLES_MSG, "error")
    finally:
        conn.close()

    if not goal:
        flash("Goal not found.", "error")
        return redirect(url_for("goals_list"))

    if request.method == "POST":
        title = (request.form.get("title") or "").strip() or None
        raw_goal = request.form.get("target_2k", "")

        try:
            target_seconds = pacing.parse_goal_2k(raw_goal)
        except ValueError:
            flash("Enter a valid 2k goal time (for example 6:15.0).", "error")
            return render_template(
                "goal_edit.html", goal=goal, today_iso=date.today().isoformat(),
                format_split=pacing.format_split,
            ), 400

        target_date = request.form.get("target_date") or ""
        if not target_date:
            flash("Choose a target date.", "error")
            return render_template(
                "goal_edit.html", goal=goal, today_iso=date.today().isoformat(),
                format_split=pacing.format_split,
            ), 400

        is_public = 0

        conn2 = get_db_connection()
        if conn2 is None:
            flash("Database unavailable.", "error")
            return render_template(
                "goal_edit.html", goal=goal, today_iso=date.today().isoformat(),
                format_split=pacing.format_split,
            ), 503

        try:
            cur = conn2.cursor()
            cur.execute(
                """
                UPDATE erg_goals
                SET title=%s, target_seconds=%s, target_date=%s, is_public=%s
                WHERE id=%s AND username=%s
                """,
                (title, target_seconds, target_date, is_public, goal_id, user),
            )
            conn2.commit()
            cur.close()
            flash("Goal updated.", "success")
            return redirect(url_for("goals_list"))
        except mysql.connector.Error as err:
            conn2.rollback()
            print(f"Goal edit error: {err}")
            flash("Could not update goal.", "error")
            return render_template(
                "goal_edit.html", goal=goal, today_iso=date.today().isoformat(),
                format_split=pacing.format_split,
            ), 500
        finally:
            conn2.close()

    return render_template(
        "goal_edit.html",
        goal=goal,
        today_iso=date.today().isoformat(),
        format_split=pacing.format_split,
    )


@login_required
@app.route("/goals/<int:goal_id>/complete", methods=["POST"])
def goal_complete(goal_id):
    user = session["user"]
    conn = get_db_connection()
    if conn is None:
        flash("Database unavailable.", "error")
        return redirect(url_for("goals_list"))

    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE erg_goals SET is_completed = 1 WHERE id = %s AND username = %s",
                (goal_id, user),
            )
        except mysql.connector.Error as col_err:
            if getattr(col_err, "errno", None) == 1054:
                _ensure_goal_completion_column(conn)
                cur.execute(
                    "UPDATE erg_goals SET is_completed = 1 WHERE id = %s AND username = %s",
                    (goal_id, user),
                )
            else:
                raise
        conn.commit()
        cur.close()
        session["celebrate"] = "goal_completed"
        flash("Goal completed! Incredible work — you crushed it.", "success")
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Goal complete error: {err}")
        flash("Could not mark goal as complete.", "error")
    finally:
        conn.close()

    return redirect(url_for("goals_list"))


@login_required
@app.route("/leaderboard")
def leaderboard():
    return redirect(url_for("dashboard"))


@login_required
@app.route("/profile", methods=["GET", "POST"])
def profile():
    user = session["user"]
    current_phone = ""
    current_two_k = ""
    current_six_k = ""
    is_coxswain = False
    predictions_enabled = False
    conn = get_db_connection()
    if conn is None:
        flash("Database unavailable.", "error")
        return render_template(
            "profile.html",
            current_phone=current_phone,
            current_two_k=current_two_k,
            current_six_k=current_six_k,
            is_coxswain=is_coxswain,
            predictions_enabled=predictions_enabled,
        )

    if request.method == "POST":
        phone_raw = (request.form.get("whatsapp_phone") or "").strip()
        phone_norm = re.sub(r"\D", "", phone_raw) or None
        is_coxswain_raw = (request.form.get("is_coxswain") or "").strip()
        if is_coxswain_raw not in ("0", "1"):
            flash("Please indicate whether you are a coxswain.", "error")
            return redirect(url_for("profile"))
        is_coxswain = is_coxswain_raw == "1"
        no_erg_times = request.form.get("no_erg_times") == "1"

        two_k_seconds = None
        six_k_seconds = None
        if is_coxswain or no_erg_times:
            if (request.form.get("two_k") or "").strip() or (request.form.get("six_k") or "").strip():
                flash("Leave 2k and 6k blank when you are a coxswain or don't have test times yet.", "error")
                return redirect(url_for("profile"))
        else:
            two_k_raw = (request.form.get("two_k") or "").strip()
            six_k_raw = (request.form.get("six_k") or "").strip()
            if not two_k_raw or not six_k_raw:
                flash("Enter both your current 2k and 6k times, or check that you don't have them yet.", "error")
                return redirect(url_for("profile"))
            two_k_seconds, err = _parse_optional_erg_time(two_k_raw, "2k")
            if err:
                flash(err, "error")
                return redirect(url_for("profile"))
            six_k_seconds, err = _parse_optional_erg_time(six_k_raw, "6k")
            if err:
                flash(err, "error")
                return redirect(url_for("profile"))
        try:
            _ensure_whatsapp_phone_column(conn)
            _ensure_user_profile_columns(conn)
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE rowing_users
                SET whatsapp_phone = %s, two_k_seconds = %s, six_k_seconds = %s, is_coxswain = %s
                WHERE username = %s
                """,
                (phone_norm, two_k_seconds, six_k_seconds, 1 if is_coxswain else 0, user),
            )
            conn.commit()
            cur.close()
            flash("Profile updated.", "success")
        except mysql.connector.Error as err:
            conn.rollback()
            print(f"Profile update error: {err}")
            flash("Could not update profile.", "error")
        finally:
            conn.close()
        return redirect(url_for("profile"))

    try:
        _ensure_whatsapp_phone_column(conn)
        _ensure_user_profile_columns(conn)
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT whatsapp_phone, two_k_seconds, six_k_seconds, is_coxswain FROM rowing_users WHERE username = %s",
            (user,),
        )
        row = cur.fetchone()
        if row:
            if row.get("whatsapp_phone"):
                current_phone = row["whatsapp_phone"]
            if row.get("two_k_seconds") is not None:
                current_two_k = pacing.format_split(float(row["two_k_seconds"]))
            if row.get("six_k_seconds") is not None:
                current_six_k = pacing.format_split(float(row["six_k_seconds"]))
            is_coxswain = bool(row.get("is_coxswain"))
            predictions_enabled = pacing.profile_supports_predictions(row)
        cur.close()
    except mysql.connector.Error as err:
        print(f"Profile load error: {err}")
    finally:
        conn.close()

    return render_template(
        "profile.html",
        current_phone=current_phone,
        current_two_k=current_two_k,
        current_six_k=current_six_k,
        is_coxswain=is_coxswain,
        predictions_enabled=predictions_enabled,
    )


def _row_workout_date_as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


_WORKOUT_CAL_TIER_COLORS = {
    5: ("#22c55e", "#0f172a", "#16a34a"),
    4: ("#84cc16", "#1a2e05", "#65a30d"),
    3: ("#eab308", "#1c1917", "#ca8a04"),
    2: ("#f97316", "#1c1917", "#ea580c"),
    1: ("#ef4444", "#fff5f5", "#dc2626"),
}


def _calendar_month_bounds(req, today: date) -> tuple[int, int, date, date]:
    y = req.args.get("year", type=int) or today.year
    m = req.args.get("month", type=int) or today.month
    if m < 1 or m > 12 or y < 1990 or y > 2105:
        y, m = today.year, today.month
    try:
        first = date(y, m, 1)
    except ValueError:
        y, m = today.year, today.month
        first = date(y, m, 1)
    if m == 12:
        last = date(y, 12, 31)
    else:
        last = date(y, m + 1, 1) - timedelta(days=1)
    return y, m, first, last


def _workout_calendar_events_for_month(user: str, first: date, last: date) -> list[dict]:
    day_stats: dict[date, dict] = {}
    conn = get_db_connection()
    if conn is None:
        flash("Unable to reach the database.", "error")
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT workout_date,
                   COUNT(*) AS n,
                   COALESCE(SUM(duration_seconds), 0) / 60.0 AS total_min
            FROM erg_workouts
            WHERE username = %s AND workout_date >= %s AND workout_date <= %s
            GROUP BY workout_date
            """,
            (user, first, last),
        )
        for row in cur.fetchall():
            dkey = _row_workout_date_as_date(row["workout_date"])
            day_stats[dkey] = {
                "count": int(row["n"]),
                "minutes": float(row["total_min"] or 0),
            }
        cur.close()
    except mysql.connector.Error as err:
        if getattr(err, "errno", None) != 1146:
            raise
        flash(TRACKER_TABLES_MSG, "error")
        return []
    finally:
        conn.close()

    events = []
    for d, info in day_stats.items():
        n = info["count"]
        mins = info["minutes"]
        title = f"{n} workout{'s' if n != 1 else ''}"
        if mins >= 1:
            title += f" · {format_minutes(mins)}"
        bg, txt, border = _WORKOUT_CAL_TIER_COLORS.get(4, _WORKOUT_CAL_TIER_COLORS[4])
        events.append({
            "id": f"workout-{d.isoformat()}",
            "title": title,
            "start": d.isoformat(),
            "allDay": True,
            "backgroundColor": bg,
            "borderColor": border,
            "textColor": txt,
        })
    return events


@login_required
@app.route("/calendar")
def workout_calendar():
    q = {k: request.args[k] for k in ("year", "month") if k in request.args}
    return redirect(url_for("workouts_list", **q))


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ── My WhatsApp scans ─────────────────────────────────────────────────────────


def _user_whatsapp_phone(conn, username: str) -> str | None:
    _ensure_whatsapp_phone_column(conn)
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT whatsapp_phone FROM rowing_users WHERE username = %s",
        (username,),
    )
    row = cur.fetchone()
    cur.close()
    phone = row.get("whatsapp_phone") if row else None
    return phone or None


def _load_user_scan(conn, username: str, scan_id: int) -> tuple[dict | None, dict | None]:
    """Return (scan row, linked workout row) if the athlete may access this scan."""
    phone = _user_whatsapp_phone(conn, username)
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM pending_whatsapp_scans WHERE id = %s", (scan_id,))
    scan = cur.fetchone()
    workout = None
    if scan and scan.get("workout_id"):
        cur.execute(
            """
            SELECT id, username, goal_id, workout_date, label, duration_seconds,
                   distance_meters, avg_split_seconds, workout_key, pace_rating,
                   expected_split_seconds, split_delta_seconds
            FROM erg_workouts WHERE id = %s
            """,
            (scan["workout_id"],),
        )
        workout = cur.fetchone()
    cur.close()

    if not scan or scan.get("status") == "rejected":
        return None, None
    if not ocr_processor.scan_belongs_to_user(
        scan,
        username,
        phone,
        workout.get("username") if workout else None,
    ):
        return None, None
    return scan, workout


@login_required
@app.route("/my-scans")
def my_scans_list():
    user = session["user"]
    rows: list = []
    conn = get_db_connection()
    if conn is None:
        flash("Unable to reach the database.", "error")
    else:
        try:
            phone = _user_whatsapp_phone(conn, user)
            sender_norm = ocr_processor.normalize_phone(phone)
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT s.id, s.received_at, s.status, s.sender_phone,
                       s.detected_split_seconds, s.detected_distance_meters, s.workout_id,
                       w.workout_date, w.avg_split_seconds AS logged_split,
                       w.distance_meters AS logged_distance, w.duration_seconds AS logged_duration,
                       w.workout_key, w.label AS workout_label,
                       g.title AS goal_title
                FROM pending_whatsapp_scans s
                LEFT JOIN erg_workouts w ON w.id = s.workout_id AND w.username = %s
                LEFT JOIN erg_goals g ON g.id = w.goal_id
                WHERE s.status != 'rejected'
                  AND (
                    s.matched_username = %s
                    OR w.id IS NOT NULL
                    OR (%s != '' AND REGEXP_REPLACE(s.sender_phone, '[^0-9]', '') = %s)
                  )
                ORDER BY s.received_at DESC
                LIMIT 80
                """,
                (user, user, sender_norm, sender_norm),
            )
            rows = cur.fetchall()
            cur.close()
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) == 1146:
                flash("WhatsApp scan tables are missing. Apply wa_schema.sql to your database.", "error")
            else:
                raise
        finally:
            conn.close()

    return render_template(
        "my_scans.html",
        scans=rows,
        format_split=pacing.format_split,
        format_minutes=format_minutes,
        is_steady_workout=pacing.is_steady_state_workout,
        effective_workout_duration=pacing.effective_workout_duration,
    )


@login_required
@app.route("/my-scans/<int:scan_id>/image")
def my_scan_image(scan_id):
    user = session["user"]
    conn = get_db_connection()
    if conn is None:
        return "Database unavailable", 503
    try:
        scan, _ = _load_user_scan(conn, user, scan_id)
    finally:
        conn.close()
    if not scan:
        return "Not found", 404
    image_path = scan.get("image_path")
    if not image_path or not Path(image_path).exists():
        return "Image not found", 404
    return send_file(image_path)


@login_required
@app.route("/my-scans/<int:scan_id>", methods=["GET", "POST"])
def my_scan_detail(scan_id):
    user = session["user"]
    chart = pacing.load_chart()
    workout_types = chart.get("workout_types", {})
    default_key = chart.get("default_steady_workout_key", "split_offset_plus_18")

    conn = get_db_connection()
    if conn is None:
        flash("Database unavailable.", "error")
        return redirect(url_for("my_scans_list"))

    scan, workout = _load_user_scan(conn, user, scan_id)
    if not scan:
        conn.close()
        flash("Scan not found or not linked to your account.", "error")
        return redirect(url_for("my_scans_list"))

    goals: list = []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, title, target_seconds, target_date
            FROM erg_goals WHERE username = %s AND is_completed = 0
            ORDER BY target_date ASC
            """,
            (user,),
        )
        goals = cur.fetchall()
        cur.close()
    except mysql.connector.Error:
        pass

    if request.method == "POST":
        split_raw = (request.form.get("avg_split") or "").strip()
        workout_key = (request.form.get("workout_key") or default_key).strip()
        goal_id_raw = (request.form.get("goal_id") or "").strip()
        workout_date = request.form.get("workout_date") or date.today().isoformat()
        dist_raw = (request.form.get("distance_meters") or "").strip()
        dur_raw = (request.form.get("duration_seconds") or "").strip()
        label = (request.form.get("label") or "").strip() or None
        if not goal_id_raw and workout and workout.get("goal_id"):
            goal_id_raw = str(workout["goal_id"])
        log_workout = bool(goal_id_raw)

        try:
            split_seconds = pacing.parse_split(split_raw)
        except ValueError:
            flash("Enter a valid split like 1:58.5.", "error")
            return redirect(url_for("my_scan_detail", scan_id=scan_id))

        distance_meters = int(dist_raw) if dist_raw.isdigit() else None
        duration_seconds = int(dur_raw) if dur_raw.isdigit() else None

        if workout_key not in workout_types:
            workout_key = default_key

        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE pending_whatsapp_scans
                SET detected_split_seconds = %s, detected_distance_meters = %s
                WHERE id = %s
                """,
                (split_seconds, distance_meters, scan_id),
            )
            conn.commit()
            cur.close()
        except mysql.connector.Error:
            conn.rollback()
            flash("Could not update scan values.", "error")
            conn.close()
            return redirect(url_for("my_scan_detail", scan_id=scan_id))

        if log_workout or workout:
            if not goal_id_raw:
                flash("Choose a goal to log or update this workout.", "error")
                conn.close()
                return redirect(url_for("my_scan_detail", scan_id=scan_id))
            try:
                goal_id = int(goal_id_raw)
            except ValueError:
                flash("Choose a valid goal.", "error")
                conn.close()
                return redirect(url_for("my_scan_detail", scan_id=scan_id))

            result = ocr_processor.save_scan_workout(
                scan_id,
                user,
                split_seconds,
                workout_key,
                goal_id,
                workout_date,
                distance_meters=distance_meters,
                duration_seconds=duration_seconds,
                label=label,
            )
            if not result.get("error"):
                _update_streak(conn, user, workout_date)
            conn.close()
            if result.get("error"):
                flash(f"Could not save workout: {result['error']}", "error")
                return redirect(url_for("my_scan_detail", scan_id=scan_id))

            if result.get("steady"):
                flash(
                    f"Workout updated — {format_minutes((result.get('duration_seconds') or 0) / 60.0)} "
                    "steady volume logged.",
                    "success",
                )
            elif result.get("expected") is not None:
                flash(
                    f"Workout updated — target split {pacing.format_split(result['expected'])}.",
                    "success",
                )
            else:
                flash("Workout saved.", "success")
            return redirect(url_for("my_scan_detail", scan_id=scan_id))

        conn.close()
        flash("Scan values updated. Choose a goal and save again to log as a workout.", "success")
        return redirect(url_for("my_scan_detail", scan_id=scan_id))

    conn.close()

    form = {
        "avg_split": "",
        "distance_meters": "",
        "duration_seconds": "",
        "workout_key": default_key,
        "goal_id": "",
        "workout_date": date.today().isoformat(),
        "label": "",
    }
    if workout:
        form["avg_split"] = pacing.format_split(float(workout["avg_split_seconds"]))
        form["distance_meters"] = workout.get("distance_meters") or ""
        form["duration_seconds"] = workout.get("duration_seconds") or ""
        form["workout_key"] = workout.get("workout_key") or default_key
        form["goal_id"] = workout.get("goal_id") or ""
        wd = workout.get("workout_date")
        form["workout_date"] = wd.isoformat() if hasattr(wd, "isoformat") else str(wd)[:10]
        form["label"] = workout.get("label") or ""
    else:
        if scan.get("detected_split_seconds") is not None:
            form["avg_split"] = pacing.format_split(float(scan["detected_split_seconds"]))
        form["distance_meters"] = scan.get("detected_distance_meters") or ""
        recv = scan.get("received_at")
        if recv:
            form["workout_date"] = recv.strftime("%Y-%m-%d") if hasattr(recv, "strftime") else str(recv)[:10]

    parsed_split = None
    if form["avg_split"]:
        try:
            parsed_split = pacing.parse_split(form["avg_split"])
        except ValueError:
            parsed_split = None
    dur_val = int(form["duration_seconds"]) if str(form["duration_seconds"]).isdigit() else None
    dist_val = int(form["distance_meters"]) if str(form["distance_meters"]).isdigit() else None
    effective_dur = (
        pacing.effective_workout_duration(dur_val, dist_val, parsed_split)
        if parsed_split is not None
        else None
    )

    return render_template(
        "my_scan_detail.html",
        scan=scan,
        workout=workout,
        goals=goals,
        form=form,
        workout_types=workout_types,
        default_key=default_key,
        effective_dur=effective_dur,
        format_split=pacing.format_split,
        format_minutes=format_minutes,
        is_steady_workout=pacing.is_steady_state_workout,
    )


# ── Coxswain team workspace ───────────────────────────────────────────────────


def _fetch_team_athletes(conn, week_start: date, week_end: date) -> list[dict]:
    """All rowers with weekly workout counts, verification backlog, and progress hints."""
    _ensure_user_profile_columns(conn)
    _ensure_workout_verification_columns(conn)
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT u.username, u.two_k_seconds, u.six_k_seconds, u.streak_count,
               g.id AS goal_id, g.title AS goal_title, g.target_seconds,
               g.target_date, g.is_completed AS goal_completed
        FROM rowing_users u
        LEFT JOIN erg_goals g ON g.id = (
            SELECT g2.id FROM erg_goals g2
            WHERE g2.username = u.username AND g2.is_completed = 0
            ORDER BY g2.target_date ASC
            LIMIT 1
        )
        WHERE u.is_coxswain = 0
        ORDER BY u.username
        """
    )
    athletes = cur.fetchall()
    today = date.today()
    thirty_ago = today - timedelta(days=30)

    for athlete in athletes:
        uname = athlete["username"]
        athlete["display_name"] = _display_name(uname)

        cur.execute(
            """
            SELECT COUNT(*) AS c FROM erg_workouts
            WHERE username = %s AND workout_date >= %s AND workout_date <= %s
            """,
            (uname, week_start, week_end),
        )
        athlete["workouts_week"] = int(cur.fetchone()["c"])
        athlete["meets_weekly_target"] = athlete["workouts_week"] >= COXSWAIN_WEEKLY_TARGET

        cur.execute(
            """
            SELECT COUNT(*) AS c FROM erg_workouts
            WHERE username = %s AND verified_at IS NULL
              AND workout_date >= %s
            """,
            (uname, thirty_ago),
        )
        athlete["unverified_count"] = int(cur.fetchone()["c"])

        cur.execute(
            """
            SELECT COALESCE(SUM(duration_seconds), 0) / 60.0 AS steady_min
            FROM erg_workouts
            WHERE username = %s
              AND workout_date >= %s AND workout_date <= %s
              AND duration_seconds > %s
            """,
            (uname, week_start, week_end, pacing.STEADY_STATE_MIN_DURATION_SECONDS),
        )
        steady_row = cur.fetchone()
        athlete["steady_minutes_week"] = int(round(float(steady_row["steady_min"] or 0)))

        cur.execute(
            """
            SELECT split_delta_seconds, pace_rating
            FROM erg_workouts
            WHERE username = %s AND workout_date >= %s
              AND (pace_rating IS NOT NULL OR split_delta_seconds IS NOT NULL)
            """,
            (uname, thirty_ago),
        )
        scores = []
        for wr in cur.fetchall():
            scores.append(
                pacing.workout_pace_score(
                    wr.get("split_delta_seconds"), wr.get("pace_rating")
                )
            )
        athlete["avg_pace_score_30d"] = (
            round(sum(scores) / len(scores), 2) if scores else None
        )

        cur.execute(
            """
            SELECT workout_date FROM erg_workouts
            WHERE username = %s ORDER BY workout_date DESC, id DESC LIMIT 1
            """,
            (uname,),
        )
        last_row = cur.fetchone()
        athlete["last_workout_date"] = last_row["workout_date"] if last_row else None

        td = athlete.get("target_date")
        athlete["goal_days_left"] = (td - today).days if td else None

        if (
            athlete.get("target_seconds") is not None
            and athlete.get("two_k_seconds") is not None
            and not athlete.get("goal_completed")
        ):
            athlete["gap_2k_seconds"] = float(athlete["two_k_seconds"]) - float(
                athlete["target_seconds"]
            )
        else:
            athlete["gap_2k_seconds"] = None

    cur.close()
    athletes.sort(
        key=lambda a: (
            not a["meets_weekly_target"],
            -(a["workouts_week"] or 0),
            a["display_name"].lower(),
        )
    )
    return athletes


def _fetch_athlete_workouts(conn, username: str, limit: int = 80) -> list[dict]:
    _ensure_workout_verification_columns(conn)
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT w.id, w.workout_date, w.label, w.avg_split_seconds, w.pace_rating,
               w.expected_split_seconds, w.split_delta_seconds, w.workout_key,
               w.duration_seconds, w.distance_meters, w.verified_at, w.verified_by,
               g.title AS goal_title
        FROM erg_workouts w
        LEFT JOIN erg_goals g ON w.goal_id = g.id
        WHERE w.username = %s
        ORDER BY w.workout_date DESC, w.id DESC
        LIMIT %s
        """,
        (username, limit),
    )
    rows = cur.fetchall()
    cur.close()
    for row in rows:
        row["pace_score"] = pacing.workout_pace_score(
            row.get("split_delta_seconds"), row.get("pace_rating")
        )
    return rows


@coxswain_required
@app.route("/coxswain")
def coxswain_workspace():
    today = date.today()
    week_start, week_end = _parse_week_param(request.args.get("week"), today)
    prev_week = week_start - timedelta(days=7)
    next_week = week_start + timedelta(days=7)
    filter_status = request.args.get("filter", "all")

    athletes: list[dict] = []
    conn = get_db_connection()
    if conn:
        try:
            athletes = _fetch_team_athletes(conn, week_start, week_end)
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) != 1146:
                raise
            flash(TRACKER_TABLES_MSG, "error")
        finally:
            conn.close()

    on_track = sum(1 for a in athletes if a["meets_weekly_target"])
    total_rowers = len(athletes)
    unverified_total = sum(a["unverified_count"] for a in athletes)

    if filter_status == "on_track":
        athletes = [a for a in athletes if a["meets_weekly_target"]]
    elif filter_status == "behind":
        athletes = [a for a in athletes if not a["meets_weekly_target"]]
    elif filter_status == "unverified":
        athletes = [a for a in athletes if a["unverified_count"] > 0]

    behind_count = total_rowers - on_track

    return render_template(
        "coxswain_workspace.html",
        athletes=athletes,
        week_start=week_start,
        week_end=week_end,
        prev_week_iso=prev_week.isoformat(),
        next_week_iso=next_week.isoformat(),
        filter_status=filter_status,
        on_track=on_track,
        behind_count=behind_count,
        total_rowers=total_rowers,
        unverified_total=unverified_total,
        weekly_target=COXSWAIN_WEEKLY_TARGET,
        today=today,
    )


@coxswain_required
@app.route("/coxswain/athlete/<path:username>")
def coxswain_athlete(username: str):
    username = username.strip()
    if not username:
        flash("Athlete not found.", "error")
        return redirect(url_for("coxswain_workspace"))

    today = date.today()
    week_start, week_end = _week_bounds_mon_sun(today)
    chart = pacing.load_chart()
    workout_types = chart.get("workout_types", {})
    profile = None
    workouts: list[dict] = []
    primary_goal = None
    goal_plan = None

    conn = get_db_connection()
    if conn is None:
        flash("Database unavailable.", "error")
        return redirect(url_for("coxswain_workspace"))

    try:
        _ensure_user_profile_columns(conn)
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT username, two_k_seconds, six_k_seconds, is_coxswain, streak_count
            FROM rowing_users WHERE username = %s
            """,
            (username,),
        )
        profile = cur.fetchone()
        cur.close()

        if not profile or profile.get("is_coxswain"):
            flash("Athlete not found.", "error")
            return redirect(url_for("coxswain_workspace"))

        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, title, target_seconds, target_date, is_completed
            FROM erg_goals WHERE username = %s
            ORDER BY is_completed ASC, target_date ASC
            """,
            (username,),
        )
        goals = cur.fetchall()
        cur.close()

        for g in goals:
            td = g.get("target_date")
            g["days_left"] = (td - today).days if td else None

        primary_goal = next((g for g in goals if not g.get("is_completed")), None)
        profile_two_k = (
            float(profile["two_k_seconds"])
            if profile.get("two_k_seconds") is not None
            else None
        )
        if primary_goal and profile_two_k is not None and primary_goal.get("target_seconds"):
            goal_plan = pacing.build_goal_plan(
                chart,
                float(primary_goal["target_seconds"]),
                profile_two_k,
                primary_goal.get("days_left"),
            )

        workouts = _fetch_athlete_workouts(conn, username)

        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT COUNT(*) AS c FROM erg_workouts
            WHERE username = %s AND workout_date >= %s AND workout_date <= %s
            """,
            (username, week_start, week_end),
        )
        workouts_week = int(cur.fetchone()["c"])
        cur.close()
    except mysql.connector.Error as err:
        if getattr(err, "errno", None) != 1146:
            raise
        flash(TRACKER_TABLES_MSG, "error")
        return redirect(url_for("coxswain_workspace"))
    finally:
        conn.close()

    verified_count = sum(1 for w in workouts if w.get("verified_at"))
    unverified_count = len(workouts) - verified_count

    return render_template(
        "coxswain_athlete.html",
        athlete=profile,
        display_name=_display_name(username),
        goals=goals,
        primary_goal=primary_goal,
        goal_plan=goal_plan,
        workouts=workouts,
        workout_types=workout_types,
        workouts_week=workouts_week,
        week_start=week_start,
        week_end=week_end,
        weekly_target=COXSWAIN_WEEKLY_TARGET,
        verified_count=verified_count,
        unverified_count=unverified_count,
    )


@coxswain_required
@app.route("/coxswain/workouts/<int:workout_id>/verify", methods=["POST"])
def coxswain_verify_workout(workout_id: int):
    verifier = session["user"]
    conn = get_db_connection()
    if conn is None:
        flash("Database unavailable.", "error")
        return redirect(url_for("coxswain_workspace"))

    redirect_to = request.form.get("next") or url_for("coxswain_workspace")
    try:
        _ensure_workout_verification_columns(conn)
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, username FROM erg_workouts WHERE id = %s",
            (workout_id,),
        )
        row = cur.fetchone()
        if not row:
            flash("Workout not found.", "error")
            return redirect(redirect_to)

        cur.execute(
            """
            UPDATE erg_workouts
            SET verified_at = NOW(), verified_by = %s
            WHERE id = %s
            """,
            (verifier, workout_id),
        )
        conn.commit()
        cur.close()
        flash(f"Verified workout for {_display_name(row['username'])}.", "success")
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Coxswain verify error: {err}")
        flash("Could not verify workout.", "error")
    finally:
        conn.close()

    return redirect(redirect_to)


@coxswain_required
@app.route("/coxswain/workouts/<int:workout_id>/unverify", methods=["POST"])
def coxswain_unverify_workout(workout_id: int):
    conn = get_db_connection()
    if conn is None:
        flash("Database unavailable.", "error")
        return redirect(url_for("coxswain_workspace"))

    redirect_to = request.form.get("next") or url_for("coxswain_workspace")
    try:
        _ensure_workout_verification_columns(conn)
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE erg_workouts
            SET verified_at = NULL, verified_by = NULL
            WHERE id = %s
            """,
            (workout_id,),
        )
        conn.commit()
        cur.close()
        flash("Verification removed.", "info")
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Coxswain unverify error: {err}")
        flash("Could not update workout.", "error")
    finally:
        conn.close()

    return redirect(redirect_to)


# ── Admin — WhatsApp scan queue ──────────────────────────────────────────────

@admin_required
@app.route("/admin/scans")
def admin_scans():
    status_filter = request.args.get("status", "")
    valid_statuses = {"pending", "matched", "rejected", "no_user", "processing"}
    scans: list = []
    counts: dict = {}

    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT status, COUNT(*) AS n FROM pending_whatsapp_scans GROUP BY status"
            )
            counts = {row["status"]: int(row["n"]) for row in cur.fetchall()}

            base_q = """
                SELECT id, image_path, sender_phone, received_at, status,
                       matched_username, detected_split_seconds,
                       detected_distance_meters, workout_id, processed_at
                FROM pending_whatsapp_scans
                {where}
                ORDER BY received_at DESC
                LIMIT 120
            """
            if status_filter in valid_statuses:
                cur.execute(base_q.format(where="WHERE status = %s"), (status_filter,))
            else:
                cur.execute(base_q.format(where=""))
            scans = cur.fetchall()
            cur.close()
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) == 1146:
                flash("Run wa_schema.sql against your database first.", "error")
            else:
                raise
        finally:
            conn.close()

    total_pending = counts.get("pending", 0)
    return render_template(
        "admin_scans.html",
        scans=scans,
        counts=counts,
        total_pending=total_pending,
        status_filter=status_filter,
        format_split=pacing.format_split,
        is_admin=True,
    )


@admin_required
@app.route("/admin/scans/process-pending", methods=["POST"])
def admin_scans_process_pending():
    result = ocr_processor.process_all_pending()
    flash(
        f"OCR batch complete — {result['processed']} processed, {result['errors']} errors.",
        "success" if result["errors"] == 0 else "error",
    )
    return redirect(url_for("admin_scans"))


@admin_required
@app.route("/admin/scans/<int:scan_id>")
def admin_scan_detail(scan_id):
    conn = get_db_connection()
    scan = None
    all_users: list = []
    goals_by_user: dict = {}

    if conn:
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT * FROM pending_whatsapp_scans WHERE id = %s", (scan_id,))
            scan = cur.fetchone()

            cur.execute("SELECT username FROM rowing_users ORDER BY username")
            all_users = [r["username"] for r in cur.fetchall()]

            for uname in all_users:
                cur.execute(
                    "SELECT id, title, target_seconds FROM erg_goals "
                    "WHERE username = %s ORDER BY target_date",
                    (uname,),
                )
                goals = cur.fetchall()
                if goals:
                    goals_by_user[uname] = goals

            cur.close()
        except mysql.connector.Error as err:
            print(f"Admin scan detail error: {err}")
            flash("Database error loading scan.", "error")
            return redirect(url_for("admin_scans"))
        finally:
            conn.close()

    if not scan:
        flash("Scan not found.", "error")
        return redirect(url_for("admin_scans"))

    chart = pacing.load_chart()
    import json as _json
    return render_template(
        "admin_scan_detail.html",
        scan=scan,
        all_users=all_users,
        goals_by_user_json=_json.dumps(
            {u: [{"id": g["id"], "title": g["title"],
                  "target_seconds": g["target_seconds"]} for g in gs]
             for u, gs in goals_by_user.items()}
        ),
        workout_types=chart.get("workout_types", {}),
        format_split=pacing.format_split,
        today_iso=date.today().isoformat(),
        is_admin=True,
    )


@admin_required
@app.route("/admin/scans/<int:scan_id>/image")
def admin_scan_image(scan_id):
    conn = get_db_connection()
    image_path = None
    if conn:
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT image_path FROM pending_whatsapp_scans WHERE id = %s", (scan_id,)
            )
            row = cur.fetchone()
            if row:
                image_path = row["image_path"]
            cur.close()
        finally:
            conn.close()

    if not image_path or not Path(image_path).exists():
        return "Image not found", 404

    return send_file(image_path)


@admin_required
@app.route("/admin/scans/<int:scan_id>/process", methods=["POST"])
def admin_scan_process(scan_id):
    result = ocr_processor.process_scan(scan_id)
    if result.get("error"):
        flash(f"OCR error: {result['error']}", "error")
    else:
        split_str = (
            pacing.format_split(result["split_seconds"])
            if result.get("split_seconds")
            else "not detected"
        )
        matched = result.get("matched_username") or "no match"
        flash(f"OCR complete — split: {split_str}, user: {matched}", "success")
    return redirect(url_for("admin_scan_detail", scan_id=scan_id))


@admin_required
@app.route("/admin/scans/<int:scan_id>/approve", methods=["POST"])
def admin_scan_approve(scan_id):
    username = (request.form.get("username") or "").strip()
    split_raw = (request.form.get("avg_split") or "").strip()
    workout_key = (request.form.get("workout_key") or "").strip()
    goal_id_raw = (request.form.get("goal_id") or "").strip()
    workout_date = request.form.get("workout_date") or date.today().isoformat()
    dist_raw = (request.form.get("distance_meters") or "").strip()
    dur_raw = (request.form.get("duration_seconds") or "").strip()
    label = (request.form.get("label") or "").strip() or None

    if not username:
        flash("Select a user account.", "error")
        return redirect(url_for("admin_scan_detail", scan_id=scan_id))

    try:
        split_seconds = pacing.parse_split(split_raw)
    except ValueError:
        flash("Enter a valid split like 1:58.5.", "error")
        return redirect(url_for("admin_scan_detail", scan_id=scan_id))

    try:
        goal_id = int(goal_id_raw)
    except (ValueError, TypeError):
        flash("Select a valid goal.", "error")
        return redirect(url_for("admin_scan_detail", scan_id=scan_id))

    distance_meters = int(dist_raw) if dist_raw.isdigit() else None
    duration_seconds = int(dur_raw) if dur_raw.isdigit() else None

    result = ocr_processor.approve_scan(
        scan_id,
        username,
        split_seconds,
        workout_key,
        goal_id,
        workout_date,
        distance_meters=distance_meters,
        duration_seconds=duration_seconds,
        label=label,
    )
    if result.get("error"):
        flash(f"Could not approve: {result['error']}", "error")
        return redirect(url_for("admin_scan_detail", scan_id=scan_id))

    streak_conn = get_db_connection()
    if streak_conn:
        try:
            _update_streak(streak_conn, username, workout_date)
        finally:
            streak_conn.close()

    if result.get("steady"):
        flash(
            f"Steady workout logged for {username} — "
            f"{format_minutes((result.get('duration_seconds') or 0) / 60.0)} toward weekly target.",
            "success",
        )
    else:
        flash(
            f"Workout logged for {username} — target split "
            f"{pacing.format_split(result['expected'])} ({result.get('rating')} vs chart).",
            "success",
        )
    return redirect(url_for("admin_scans"))


@admin_required
@app.route("/admin/scans/<int:scan_id>/reject", methods=["POST"])
def admin_scan_reject(scan_id):
    notes = (request.form.get("notes") or "").strip() or None
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE pending_whatsapp_scans "
                "SET status='rejected', admin_notes=%s, processed_at=NOW() "
                "WHERE id = %s",
                (notes, scan_id),
            )
            conn.commit()
            cur.close()
            flash("Scan rejected.", "success")
        except mysql.connector.Error as err:
            conn.rollback()
            print(f"Scan reject error: {err}")
            flash("Could not reject scan.", "error")
        finally:
            conn.close()
    return redirect(url_for("admin_scans"))


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
