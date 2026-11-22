"""
db.py - PostgreSQL access shared by web_service.py, worker.py and enrich_companies.py.

Connection settings come ONLY from environment variables:
    DB_HOST (default localhost), DB_PORT (default 5432), DB_NAME, DB_USER, DB_PASSWORD (required)
    DB_WAIT_TIMEOUT - seconds to wait for the database at startup (default 60)

init_schema() creates / upgrades every table (idempotent, safe to call from several processes at
once - an advisory lock serializes them):
    videos      - Task 1/2 (DDL below) + the zoom processing columns
    companies   - Task 3 (schema.sql)
"""

import os
import time

import psycopg2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_FILE = os.path.join(BASE_DIR, "schema.sql")

VIDEOS_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id SERIAL PRIMARY KEY,
    company_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,   -- absolute path to the file on disk
    video_url TEXT NOT NULL,   -- public URL to the video
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

# Zoom processing columns (Internship Task 2). Idempotent, so an existing database volume
# created by an older version is upgraded in place.
#   process_status: none -> (pending -> processing -> done | failed)
VIDEOS_MIGRATIONS = [
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS process_status TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS process_error TEXT",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_file_path TEXT",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_video_url TEXT",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ",
]

# any constant: makes concurrent processes create the tables one after another
SCHEMA_LOCK_ID = 727274


def db_settings():
    """Read the connection settings from the environment (nothing is hard-coded)."""
    missing = [n for n in ("DB_NAME", "DB_USER", "DB_PASSWORD") if not os.environ.get(n)]
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


def connect():
    """Open a NEW connection (psycopg2 connections must not be shared between threads)."""
    return psycopg2.connect(connect_timeout=5, **db_settings())


def wait_for_db(timeout_seconds=None):
    """Block until PostgreSQL accepts connections (it may still be starting)."""
    if timeout_seconds is None:
        timeout_seconds = int(os.environ.get("DB_WAIT_TIMEOUT", "60"))
    deadline = time.time() + timeout_seconds
    while True:
        try:
            connect().close()
            return
        except psycopg2.OperationalError as exc:
            if time.time() >= deadline:
                raise
            reason = (str(exc).strip().splitlines() or ["?"])[0]
            print("[db] waiting for the database: " + reason, flush=True)
            time.sleep(2)


def init_schema():
    """Create / upgrade all tables (videos + migrations, companies from schema.sql)."""
    with open(SCHEMA_FILE, "r", encoding="utf-8") as handle:
        companies_sql = handle.read()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            cur.execute(VIDEOS_SCHEMA)
            for statement in VIDEOS_MIGRATIONS:
                cur.execute(statement)
            cur.execute(companies_sql)
        conn.commit()
    finally:
        conn.close()
