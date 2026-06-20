"""Shared MySQL connection configuration"""

from pathlib import Path

import mysql.connector
import os

BASE_DIR = Path(__file__).resolve().parent

DOSQL_USER = os.environ.get("DOSQL_USER")
DOSQL_PASSWORD = os.environ.get("DOSQL_PASSWORD")
DOSQL_HOST = os.environ.get("DOSQL_HOST")
DOSQL_DATABASE = os.environ.get("DOSQL_DATABASE")
DOSQL_PORT = os.environ.get("DOSQL_PORT")

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
