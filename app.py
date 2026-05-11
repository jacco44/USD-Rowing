"""Serve the USD-Rowing login page with MySQL-backed authentication."""

import calendar
import os
import secrets
from datetime import date, datetime, timedelta
from datetime import date as date_class  # alias used in goals_list for clarity
from functools import wraps

import mysql.connector
from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

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
REGISTER_TEMPLATE_CTX = {"min_password_length": MIN_PASSWORD_LENGTH}


def is_valid_usd_email(email: str) -> bool:
    email = email.strip()
    return bool(email) and email.lower().endswith(USD_EMAIL_SUFFIX.lower())


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.jinja_env.globals["format_split"] = pacing.format_split

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


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


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
            return render_template("register.html", **REGISTER_TEMPLATE_CTX), 400

        if len(password) < MIN_PASSWORD_LENGTH:
            flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", "error")
            return render_template("register.html", **REGISTER_TEMPLATE_CTX), 400

        if password != password_confirm:
            flash("Passwords do not match.", "error")
            return render_template("register.html", **REGISTER_TEMPLATE_CTX), 400

        email_norm = username.lower()
        conn = get_db_connection()
        if conn is None:
            flash("Unable to reach the database. Please try again later.", "error")
            return render_template("register.html", **REGISTER_TEMPLATE_CTX), 503

        cur = None
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT 1 FROM rowing_users WHERE LOWER(username) = LOWER(%s)",
                (email_norm,),
            )
            if cur.fetchone():
                flash("An account with this email already exists.", "error")
                return render_template("register.html", **REGISTER_TEMPLATE_CTX), 409

            cur.execute(
                "INSERT INTO rowing_users (username, password) VALUES (%s, %s)",
                (email_norm, generate_password_hash(password)),
            )
            conn.commit()
            session["user"] = email_norm
            flash("Welcome! Your account is ready.", "success")
            return redirect(url_for("dashboard"))
        except mysql.connector.Error as err:
            conn.rollback()
            print(f"Registration database error: {err}")
            flash("Could not complete registration. Please try again.", "error")
            return render_template("register.html", **REGISTER_TEMPLATE_CTX), 500
        finally:
            if cur is not None:
                cur.close()
            conn.close()

    return render_template("register.html", **REGISTER_TEMPLATE_CTX)


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
                SELECT id, workout_date, avg_split_seconds, pace_rating, label, workout_key
                FROM erg_workouts WHERE username = %s
                ORDER BY workout_date DESC, id DESC LIMIT 6
                """,
                (user,),
            )
            recent_workouts = cur.fetchall()
            cur.execute(
                """
                SELECT w.username, AVG(w.pace_rating) AS ar, COUNT(*) AS n
                FROM erg_workouts w
                INNER JOIN erg_goals g ON w.goal_id = g.id AND g.is_public = 1
                WHERE w.workout_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                GROUP BY w.username
                ORDER BY ar DESC, n DESC
                LIMIT 5
                """,
            )
            leaderboard_preview = cur.fetchall()
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

    chart = pacing.load_chart()
    return render_template(
        "workouts.html",
        workouts=rows,
        workout_types=chart.get("workout_types", {}),
        format_split=pacing.format_split,
        rating_label=pacing.rating_label,
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


@login_required
@app.route("/leaderboard")
def leaderboard():
    rows = []
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT w.username,
                       AVG(w.pace_rating) AS avg_rating,
                       COUNT(*) AS workouts,
                       MAX(w.workout_date) AS last_workout
                FROM erg_workouts w
                INNER JOIN erg_goals g ON w.goal_id = g.id AND g.is_public = 1
                WHERE w.workout_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                GROUP BY w.username
                ORDER BY avg_rating DESC, workouts DESC
                LIMIT 40
                """,
            )
            rows = cur.fetchall()
            cur.close()
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) != 1146:
                raise
            flash(TRACKER_TABLES_MSG, "error")
        finally:
            conn.close()

    return render_template("leaderboard.html", rows=rows)


def _row_workout_date_as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


@login_required
@app.route("/calendar")
def workout_calendar():
    user = session["user"]
    today = date.today()
    y = request.args.get("year", type=int) or today.year
    m = request.args.get("month", type=int) or today.month
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

    if m == 1:
        prev_y, prev_m = y - 1, 12
    else:
        prev_y, prev_m = y, m - 1
    if m == 12:
        next_y, next_m = y + 1, 1
    else:
        next_y, next_m = y, m + 1

    scores: dict[date, int] = {}
    tables_ok = True
    conn = get_db_connection()
    if conn is None:
        flash("Unable to reach the database.", "error")
        tables_ok = False
    else:
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT workout_date, MIN(pace_rating) AS day_tier
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
            tables_ok = False
        finally:
            conn.close()

    cal_weeks: list[list[dict]] = []
    for week in calendar.monthcalendar(y, m):
        wrow: list[dict] = []
        for d in week:
            if d == 0:
                wrow.append({"pad": True})
                continue
            cell_dt = date(y, m, d)
            has_workout = tables_ok and cell_dt in scores
            tier = scores[cell_dt] if has_workout else 4
            if has_workout:
                tip = f"{cell_dt.isoformat()} — {tier} · {pacing.rating_label(tier)}"
            else:
                tip = f"{cell_dt.isoformat()} — Rest / no data ({pacing.rating_label(4)} color)"
            wrow.append(
                {
                    "pad": False,
                    "day": d,
                    "tier": tier,
                    "title": tip,
                    "is_today": cell_dt == today,
                }
            )
        cal_weeks.append(wrow)

    month_title = first.strftime("%B %Y")
    return render_template(
        "calendar.html",
        cal_weeks=cal_weeks,
        month_title=month_title,
        cal_year=y,
        cal_month=m,
        prev_y=prev_y,
        prev_m=prev_m,
        next_y=next_y,
        next_m=next_m,
        rating_label=pacing.rating_label,
    )


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
