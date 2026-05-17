"""Shared MySQL connection configuration"""

from pathlib import Path

import mysql.connector
import os

BASE_DIR = Path(__file__).resolve().parent
doSQL_password_file = BASE_DIR / ".passwords" / "DigitalOceanSQL.txt"

try:
    with open(doSQL_password_file, encoding="utf-8") as f:
        lines = f.read().splitlines()
        DOSQL_USER = lines[0]
        DOSQL_PASSWORD = os.environ.get("DOSQL_PASSWORD")
        DOSQL_HOST = os.environ.get("DOSQL_HOST")
        DOSQL_DATABASE = lines[3]
        DOSQL_PORT = int(lines[4].strip()) if len(lines) > 4 and lines[4].strip() else 3306
except FileNotFoundError:
    DOSQL_USER = "root"
    DOSQL_PASSWORD = ""
    DOSQL_HOST = "localhost"
    DOSQL_DATABASE = "credentialing"
    DOSQL_PORT = 3306
    print(
        f"Warning: Database password file not found at {doSQL_password_file}. "
        "Using default configuration."
    )

config = {
    "user": DOSQL_USER,
    "password": DOSQL_PASSWORD,
    "host": DOSQL_HOST,
    "database": DOSQL_DATABASE,
    "port": DOSQL_PORT,
}


def get_db_connection():
    """Create isolated database connection from global configuration."""
    try:
        return mysql.connector.connect(**config)
    except mysql.connector.Error as err:
        print(f"Database connection error: {err}")
        return None
