"""Serve the USD-Rowing login page with MySQL-backed authentication."""

import json
import os
import re
import secrets
from collections import defaultdict
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


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.jinja_env.globals["format_split"] = pacing.format_split
app.jinja_env.globals["format_pace_score"] = pacing.format_pace_score
app.jinja_env.globals["workout_pace_score"] = pacing.workout_pace_score


@app.context_processor
def inject_admin_flag():
    return {"is_admin": is_admin()}

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

    return {
        "two_k": (request.form.get("two_k", "") or "").strip() if request.method == "POST" else "",
        "hr_zone1_max": zone_field(1),
        "hr_zone2_max": zone_field(2),
        "hr_zone3_max": zone_field(3),
        "hr_zone4_max": zone_field(4),
        "hr_zone5_max": zone_field(5),
    }


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
        two_k_seconds = None
        if two_k_raw:
            try:
                two_k_seconds = int(round(pacing.parse_goal_2k(two_k_raw)))
            except ValueError:
                flash("Enter a valid current 2k time (for example 6:45.0), or leave it blank.", "error")
                return render_template(
                    "register.html", reg_form=_registration_form_values(), **REGISTER_TEMPLATE_CTX
                ), 400
            if two_k_seconds < 300 or two_k_seconds > 1500:
                flash("2k time looks unrealistic; use mm:ss between about 5:00 and 25:00.", "error")
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
                "INSERT INTO rowing_users (username, password, two_k_seconds, "
                "hr_zone1_max, hr_zone2_max, hr_zone3_max, hr_zone4_max, hr_zone5_max, whatsapp_phone) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            )
            insert_params = (
                email_norm,
                generate_password_hash(password),
                two_k_seconds,
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
    stats = {"goals": 0, "workouts_week": 0, "avg_rating": None, "best_rating_week": None, "streak": 0}
    recent_workouts = []
    leaderboard_preview = []
    conn = get_db_connection()
    if conn is None:
        flash("Unable to reach the database.", "error")
    else:
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT COUNT(*) AS c FROM erg_goals WHERE username = %s",
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
                SELECT AVG(pace_rating) AS a FROM erg_workouts
                WHERE username = %s AND workout_date >= DATE_SUB(CURDATE(), INTERVAL 14 DAY)
                """,
                (user,),
            )
            row = cur.fetchone()
            if row and row["a"] is not None:
                stats["avg_rating"] = float(row["a"])
            cur.execute(
                """
                SELECT MAX(pace_rating) AS best FROM erg_workouts
                WHERE username = %s AND workout_date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                """,
                (user,),
            )
            row = cur.fetchone()
            if row and row["best"] is not None:
                stats["best_rating_week"] = int(row["best"])
            cur.execute(
                """
                SELECT id, workout_date, avg_split_seconds, pace_rating, split_delta_seconds,
                       label, workout_key
                FROM erg_workouts WHERE username = %s
                ORDER BY workout_date DESC, id DESC LIMIT 6
                """,
                (user,),
            )
            recent_workouts = cur.fetchall()
            leaderboard_preview = _leaderboard_rows(limit=5, days=30)
            cur.close()
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
        leaderboard_preview=leaderboard_preview,
        celebrate=celebrate,
        format_split=pacing.format_split,
        rating_label=pacing.rating_label,
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
    return render_template(
        "goals.html",
        goals=rows,
        format_split=pacing.format_split,
        today=date_class.today(),
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

        is_public = 1 if request.form.get("is_public") else 0

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
        rating_label=pacing.rating_label,
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
            flash("Create a goal first so we can score your split against the pacing chart.", "error")
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

            rating = pacing.pace_rating(actual_split, expected)
            delta = actual_split - expected

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
            if rating == 5:
                session["celebrate"] = "perfect_workout"
            flash(
                f"Workout logged — {pacing.rating_label(rating)} "
                f"(target split {pacing.format_split(expected)}).",
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

        is_public = 1 if request.form.get("is_public") else 0

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


def _leaderboard_rows(limit: int = 40, days: int = 30) -> list[dict]:
    """Aggregate public-goal workout scores (continuous 1.00–5.00) per athlete."""
    conn = get_db_connection()
    if conn is None:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT w.username, w.split_delta_seconds, w.pace_rating, w.workout_date
            FROM erg_workouts w
            INNER JOIN erg_goals g ON w.goal_id = g.id AND g.is_public = 1
            WHERE w.workout_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            """,
            (days,),
        )
        raw = cur.fetchall()
        cur.close()
    except mysql.connector.Error as err:
        if getattr(err, "errno", None) != 1146:
            raise
        flash(TRACKER_TABLES_MSG, "error")
        return []
    finally:
        conn.close()

    buckets: dict[str, dict] = defaultdict(lambda: {"scores": [], "last": None})
    for row in raw:
        user = row["username"]
        buckets[user]["scores"].append(
            pacing.workout_pace_score(row.get("split_delta_seconds"), row.get("pace_rating"))
        )
        wd = row["workout_date"]
        if buckets[user]["last"] is None or wd > buckets[user]["last"]:
            buckets[user]["last"] = wd

    rows = []
    for username, data in buckets.items():
        if not data["scores"]:
            continue
        avg = sum(data["scores"]) / len(data["scores"])
        rows.append(
            {
                "username": username,
                "avg_rating": avg,
                "workouts": len(data["scores"]),
                "last_workout": data["last"],
            }
        )
    rows.sort(key=lambda r: (-r["avg_rating"], -r["workouts"]))
    return rows[:limit]


@login_required
@app.route("/leaderboard")
def leaderboard():
    rows = _leaderboard_rows(limit=40, days=30)
    return render_template(
        "leaderboard.html",
        rows=rows,
        scoring_method=pacing.SCORING_METHOD,
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
    scores: dict[date, int] = {}
    conn = get_db_connection()
    if conn is None:
        flash("Unable to reach the database.", "error")
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT workout_date, ROUND(AVG(pace_rating)) AS day_tier
            FROM erg_workouts
            WHERE username = %s AND workout_date >= %s AND workout_date <= %s
            GROUP BY workout_date
            """,
            (user, first, last),
        )
        for row in cur.fetchall():
            dkey = _row_workout_date_as_date(row["workout_date"])
            t = int(row["day_tier"])
            scores[dkey] = max(1, min(5, t))
        cur.close()
    except mysql.connector.Error as err:
        if getattr(err, "errno", None) != 1146:
            raise
        flash(TRACKER_TABLES_MSG, "error")
        return []
    finally:
        conn.close()

    events = []
    for d, tier in scores.items():
        bg, txt, border = _WORKOUT_CAL_TIER_COLORS.get(tier, _WORKOUT_CAL_TIER_COLORS[4])
        events.append({
            "id": f"workout-{d.isoformat()}",
            "title": f"Score {tier} — {pacing.rating_label(tier)}",
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


# ── Profile ─────────────────────────────────────────────────────────────────

@login_required
@app.route("/profile", methods=["GET", "POST"])
def profile():
    user = session["user"]
    current_phone = ""
    conn = get_db_connection()
    if conn is None:
        flash("Database unavailable.", "error")
        return render_template("profile.html", current_phone=current_phone)

    if request.method == "POST":
        phone_raw = (request.form.get("whatsapp_phone") or "").strip()
        phone_norm = re.sub(r"\D", "", phone_raw) or None
        try:
            _ensure_whatsapp_phone_column(conn)
            cur = conn.cursor()
            cur.execute(
                "UPDATE rowing_users SET whatsapp_phone = %s WHERE username = %s",
                (phone_norm, user),
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
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT whatsapp_phone FROM rowing_users WHERE username = %s", (user,)
        )
        row = cur.fetchone()
        if row and row.get("whatsapp_phone"):
            current_phone = row["whatsapp_phone"]
        cur.close()
    except mysql.connector.Error as err:
        print(f"Profile load error: {err}")
    finally:
        conn.close()

    return render_template("profile.html", current_phone=current_phone)


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

    result = ocr_processor.approve_scan(
        scan_id,
        username,
        split_seconds,
        workout_key,
        goal_id,
        workout_date,
        distance_meters=distance_meters,
        label=label,
    )
    if result.get("error"):
        flash(f"Could not approve: {result['error']}", "error")
        return redirect(url_for("admin_scan_detail", scan_id=scan_id))

    flash(
        f"Workout logged for {username} — rating {result['rating']} "
        f"(expected {pacing.format_split(result['expected'])}).",
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
