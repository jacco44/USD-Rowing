"""
OCR processing for WhatsApp workout images.

Handles Concept2 PM5 erg screens and queues them into the
pending_whatsapp_scans table for admin review.

Dependencies: boto3  (pip install boto3)

Optional env vars:
  AWS_DEFAULT_REGION  — defaults to "us-east-1" if not set
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import mysql.connector

import pacing
from database import get_db_connection

# ---------------------------------------------------------------------------
# AWS Textract — lazy singleton
# ---------------------------------------------------------------------------
_textract_client = None


def _get_textract_client():
    """Return a boto3 Textract client, initialised once."""
    global _textract_client
    if _textract_client is not None:
        return _textract_client

    try:
        import boto3  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is not installed. Run: pip install boto3"
        ) from exc

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    _textract_client = boto3.client("textract", region_name=region)
    return _textract_client

# ---------------------------------------------------------------------------
# Text-extraction helpers
# ---------------------------------------------------------------------------

# Concept2 PM5 split format: M:SS.d  (e.g. 1:47.3, 2:05.8)
# Realistic college-athlete range per 500 m: 1:20 (80 s) → 3:30 (210 s).
_SPLIT_RE = re.compile(r"\b([1-9]):([0-5]\d)\.(\d)\b")

# Distance as shown on PM5: "4,523 m", "10000m", "2k", etc.
_DIST_COMMA_RE = re.compile(r"\b(\d{1,2}[,]\d{3})\s*m\b", re.IGNORECASE)
_DIST_PLAIN_RE = re.compile(r"\b(\d{3,5})\s*m\b", re.IGNORECASE)
_DIST_K_RE = re.compile(r"\b(\d+(?:\.\d)?)\s*k\b", re.IGNORECASE)

# Common OCR confusion pairs for LCD-style digits — applied before matching
_LCD_FIXES = str.maketrans(
    {
        "O": "0",
        "l": "1",
        "I": "1",
        "|": "1",
        "S": "5",
    }
)


def _clean(raw_text: str) -> str:
    """Apply LCD-font normalisations."""
    return raw_text.translate(_LCD_FIXES)


def extract_split(text: str) -> float | None:
    """
    Return the first plausible per-500m split in *seconds* found in text,
    or None.

    Strategy: collect all M:SS.d matches, filter to 80–210 s, return the
    one whose value is most central to typical rowing pace (prefer ~120 s).
    """
    text = _clean(text)
    candidates: list[float] = []
    for m in _SPLIT_RE.finditer(text):
        mins, secs, tenths = int(m.group(1)), int(m.group(2)), int(m.group(3))
        total = mins * 60 + secs + tenths / 10.0
        if 80.0 <= total <= 210.0:
            candidates.append(total)

    if not candidates:
        return None
    return min(candidates, key=lambda v: abs(v - 120.0))


def extract_distance(text: str) -> int | None:
    """Return distance in metres if found, else None."""
    text = _clean(text)

    for m in _DIST_COMMA_RE.finditer(text):
        d = int(m.group(1).replace(",", ""))
        if 500 <= d <= 50_000:
            return d

    for m in _DIST_PLAIN_RE.finditer(text):
        d = int(m.group(1))
        if 500 <= d <= 50_000:
            return d

    for m in _DIST_K_RE.finditer(text):
        d = int(float(m.group(1)) * 1000)
        if 500 <= d <= 50_000:
            return d

    return None


def run_ocr(image_path: str) -> str:
    """
    Run AWS Textract DetectDocumentText on an image and return all detected
    lines joined into a single string.

    Textract handles natural-scene photos (e.g. a phone photo of a Concept2
    PM5 display) well via its LINE blocks, which are returned in reading order.
    """
    client = _get_textract_client()

    with open(image_path, "rb") as f:
        content = f.read()

    response = client.detect_document_text(Document={"Bytes": content})

    lines = [
        block["Text"]
        for block in response.get("Blocks", [])
        if block.get("BlockType") == "LINE"
    ]
    return "\n".join(lines)


def _normalize_phone(phone: str) -> str:
    """Strip all non-digit characters."""
    return re.sub(r"\D", "", phone or "")


def normalize_phone(phone: str | None) -> str:
    return _normalize_phone(phone or "")


def scan_belongs_to_user(
    scan: dict[str, Any],
    username: str,
    whatsapp_phone: str | None,
    workout_username: str | None = None,
) -> bool:
    """True when a WhatsApp scan is associated with the logged-in athlete."""
    if scan.get("matched_username") == username:
        return True
    if workout_username == username:
        return True
    user_phone = normalize_phone(whatsapp_phone)
    sender = normalize_phone(scan.get("sender_phone"))
    return bool(user_phone and sender and user_phone == sender)


def _prepare_workout_values(
    chart: dict[str, Any],
    goal_target_seconds: float,
    split_seconds: float,
    workout_key: str,
    duration_seconds: int | None,
    distance_meters: int | None,
) -> dict[str, Any]:
    effective_dur = pacing.effective_workout_duration(
        duration_seconds, distance_meters, split_seconds
    )
    stored_duration = duration_seconds
    if stored_duration is None and effective_dur is not None:
        stored_duration = effective_dur

    is_steady = pacing.is_steady_state_workout(effective_dur)
    expected = None
    if not is_steady:
        expected = pacing.expected_split_for_workout(
            chart, float(goal_target_seconds), workout_key
        )

    rating, expected, delta = pacing.workout_scoring_fields(
        split_seconds, expected, stored_duration, distance_meters
    )
    return {
        "duration_seconds": stored_duration,
        "effective_duration": effective_dur,
        "is_steady": is_steady,
        "pace_rating": rating,
        "expected_split_seconds": expected,
        "split_delta_seconds": delta,
    }


def save_scan_workout(
    scan_id: int,
    username: str,
    split_seconds: float,
    workout_key: str,
    goal_id: int,
    workout_date: str,
    distance_meters: int | None = None,
    duration_seconds: int | None = None,
    label: str | None = None,
    *,
    update_detected: bool = True,
) -> dict[str, Any]:
    """
    Create or update the erg_workouts record linked to a WhatsApp scan.
    Returns {workout_id, rating, expected, steady, duration_seconds, created} or {error}.
    """
    chart = pacing.load_chart()
    conn = get_db_connection()
    if conn is None:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM pending_whatsapp_scans WHERE id = %s", (scan_id,))
        scan = cur.fetchone()
        if not scan:
            return {"error": "Scan not found"}

        cur.execute(
            "SELECT id, target_seconds FROM erg_goals WHERE id = %s AND username = %s",
            (goal_id, username),
        )
        goal = cur.fetchone()
        if not goal:
            return {"error": "Goal not found for this user"}

        values = _prepare_workout_values(
            chart,
            float(goal["target_seconds"]),
            split_seconds,
            workout_key,
            duration_seconds,
            distance_meters,
        )
        if not values["is_steady"] and values["expected_split_seconds"] is None:
            return {"error": "Cannot compute expected split from pacing chart"}

        workout_label = label or "WhatsApp import"
        note_suffix = f"WhatsApp scan #{scan_id}"
        existing_id = scan.get("workout_id")
        created = False

        if existing_id:
            cur.execute(
                "SELECT id FROM erg_workouts WHERE id = %s AND username = %s",
                (existing_id, username),
            )
            if not cur.fetchone():
                return {"error": "Linked workout not found for your account"}

            cur.execute(
                """
                UPDATE erg_workouts
                SET goal_id=%s, workout_date=%s, label=%s, duration_seconds=%s,
                    distance_meters=%s, avg_split_seconds=%s, workout_key=%s,
                    pace_rating=%s, expected_split_seconds=%s, split_delta_seconds=%s,
                    notes=%s
                WHERE id=%s AND username=%s
                """,
                (
                    goal_id,
                    workout_date,
                    workout_label,
                    values["duration_seconds"],
                    distance_meters,
                    split_seconds,
                    workout_key,
                    values["pace_rating"],
                    values["expected_split_seconds"],
                    values["split_delta_seconds"],
                    note_suffix,
                    existing_id,
                    username,
                ),
            )
            workout_id = int(existing_id)
        else:
            cur.execute(
                """
                INSERT INTO erg_workouts (
                    username, goal_id, workout_date, label,
                    duration_seconds, distance_meters, avg_split_seconds, workout_key,
                    pace_rating, expected_split_seconds, split_delta_seconds, notes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    username,
                    goal_id,
                    workout_date,
                    workout_label,
                    values["duration_seconds"],
                    distance_meters,
                    split_seconds,
                    workout_key,
                    values["pace_rating"],
                    values["expected_split_seconds"],
                    values["split_delta_seconds"],
                    note_suffix,
                ),
            )
            workout_id = int(cur.lastrowid)
            created = True

        scan_updates = (
            "status='matched', workout_id=%s, matched_username=%s, processed_at=NOW()"
        )
        scan_params: list[Any] = [workout_id, username]
        if update_detected:
            scan_updates += ", detected_split_seconds=%s, detected_distance_meters=%s"
            scan_params.extend([split_seconds, distance_meters])
        scan_params.append(scan_id)
        cur.execute(
            f"UPDATE pending_whatsapp_scans SET {scan_updates} WHERE id = %s",
            tuple(scan_params),
        )
        conn.commit()
        return {
            "workout_id": workout_id,
            "rating": values["pace_rating"],
            "expected": values["expected_split_seconds"],
            "steady": values["is_steady"],
            "duration_seconds": values["effective_duration"],
            "created": created,
        }

    except mysql.connector.Error as err:
        conn.rollback()
        return {"error": str(err)}

    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass
        conn.close()


# ---------------------------------------------------------------------------
# Public API — called from Flask routes
# ---------------------------------------------------------------------------

def process_scan(scan_id: int) -> dict[str, Any]:
    """
    Run OCR on one pending scan.  Updates the DB row and returns a result
    dict with keys: ocr_text, split_seconds, distance_meters,
    matched_username, status.  On error returns {error: str}.
    """
    conn = get_db_connection()
    if conn is None:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM pending_whatsapp_scans WHERE id = %s", (scan_id,))
        scan = cur.fetchone()
        if not scan:
            return {"error": "Scan not found"}

        image_path = scan["image_path"]
        if not Path(image_path).exists():
            cur.execute(
                "UPDATE pending_whatsapp_scans "
                "SET status='rejected', admin_notes='Image file missing', processed_at=NOW() "
                "WHERE id = %s",
                (scan_id,),
            )
            conn.commit()
            return {"error": "Image file not found on disk"}

        cur.execute(
            "UPDATE pending_whatsapp_scans SET status='processing' WHERE id = %s",
            (scan_id,),
        )
        conn.commit()

        try:
            ocr_text = run_ocr(image_path)
        except Exception as exc:  # noqa: BLE001
            cur.execute(
                "UPDATE pending_whatsapp_scans "
                "SET status='pending', admin_notes=%s WHERE id = %s",
                (f"OCR error: {exc}", scan_id),
            )
            conn.commit()
            return {"error": f"OCR failed: {exc}"}

        split_seconds = extract_split(ocr_text)
        distance_meters = extract_distance(ocr_text)

        sender_norm = _normalize_phone(scan["sender_phone"])
        cur.execute(
            "SELECT username FROM rowing_users "
            "WHERE REGEXP_REPLACE(whatsapp_phone, '[^0-9]', '') = %s "
            "LIMIT 1",
            (sender_norm,),
        )
        user_row = cur.fetchone()
        matched_username = user_row["username"] if user_row else None

        new_status = "matched" if (matched_username and split_seconds) else "no_user"

        cur.execute(
            """
            UPDATE pending_whatsapp_scans
            SET ocr_raw_text        = %s,
                detected_split_seconds   = %s,
                detected_distance_meters = %s,
                matched_username         = %s,
                status                   = %s,
                processed_at             = NOW()
            WHERE id = %s
            """,
            (ocr_text, split_seconds, distance_meters, matched_username, new_status, scan_id),
        )
        conn.commit()

        return {
            "ocr_text": ocr_text,
            "split_seconds": split_seconds,
            "distance_meters": distance_meters,
            "matched_username": matched_username,
            "status": new_status,
        }

    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass
        conn.close()


def process_all_pending() -> dict[str, int]:
    """OCR every scan that has never been processed. Returns {processed, errors}."""
    conn = get_db_connection()
    if conn is None:
        return {"processed": 0, "errors": 0}

    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id FROM pending_whatsapp_scans
            WHERE status = 'pending'
              AND ocr_raw_text IS NULL
              AND (admin_notes IS NULL OR admin_notes NOT LIKE 'OCR error:%')
            """
        )
        ids = [row["id"] for row in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    processed = 0
    errors = 0
    for scan_id in ids:
        result = process_scan(scan_id)
        if result.get("error"):
            errors += 1
        else:
            processed += 1

    return {"processed": processed, "errors": errors}


def approve_scan(
    scan_id: int,
    username: str,
    split_seconds: float,
    workout_key: str,
    goal_id: int,
    workout_date: str,
    distance_meters: int | None = None,
    duration_seconds: int | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Convert an approved scan into an erg_workouts record (admin)."""
    return save_scan_workout(
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
