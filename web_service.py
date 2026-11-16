"""
web_service.py - lightweight web service + database layer (Flask + PostgreSQL).

Endpoints:
    POST /upload                         UploadVideo  (multipart: file, company_name)
    GET  /get_video_url?company_name=X   GetVideoURL
    GET  /media/<filename>               serves an uploaded video file
    GET  /videos                         (optional) lists all records
    GET  /health                         (optional) liveness + database check

Zoom processing (Internship Task 2) - the zoom itself runs in worker.py (ffmpeg):
    POST /upload                         accepts an optional multipart field "zoom" (the
                                         <video>.zoom.json sidecar) -> the video is queued
    POST /jobs/<id>/process              (re)queue the zoom processing of a video
    GET  /jobs[?company_name=X]          processing jobs (status, processed_video_url)
    GET  /jobs/<id>                      one job
    GET  /get_processed_video_url?company_name=X   URL of the zoomed video (404 until ready)

In Docker the service runs under Apache + mod_wsgi (see app.wsgi), the database is
a separate PostgreSQL container. Local run (needs a reachable PostgreSQL):
    python web_service.py
Host / port: FLASK_HOST / FLASK_PORT (default 0.0.0.0:8000).

Database connection settings come ONLY from environment variables:
    DB_HOST (default localhost), DB_PORT (default 5432),
    DB_NAME, DB_USER, DB_PASSWORD (required)
Optional: DB_WAIT_TIMEOUT (seconds to wait for the database at startup, default 60),
          UPLOAD_DIR (default ./uploads next to this file).

The database stores the company name, the absolute file path and the video URL -
never the video bytes. Files live in the uploads/ folder (a Docker volume).
"""

import hashlib
import json
import os
import re
import time
from urllib.parse import quote

import psycopg2
import psycopg2.extras
from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(BASE_DIR, "uploads")

SCHEMA = """
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
# created by the previous version is upgraded in place.
#   process_status: none -> (pending -> processing -> done | failed)
MIGRATIONS = [
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS process_status TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS process_error TEXT",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_file_path TEXT",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_video_url TEXT",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ",
]

JOB_COLUMNS = ("id, company_name, process_status, process_error, "
               "video_url, processed_video_url, processed_at, updated_at")

# any constant: makes concurrent workers create the table one after another
SCHEMA_LOCK_ID = 727274

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024    # 500 MB upload limit


# ---------------------------------------------------------------------------
# Database layer (PostgreSQL via psycopg2)
# ---------------------------------------------------------------------------
def _db_settings():
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


def _connect():
    """Open a NEW connection (psycopg2 connections must not be shared between threads)."""
    return psycopg2.connect(connect_timeout=5, **_db_settings())


def get_db():
    """Per-request connection stored in flask.g, closed in teardown."""
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()                    # an uncommitted transaction is rolled back


def query_one(db, sql, params=()):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def query_all(db, sql, params=()):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def wait_for_db(timeout_seconds):
    """Block until PostgreSQL accepts connections (it may still be starting)."""
    deadline = time.time() + timeout_seconds
    while True:
        try:
            _connect().close()
            return
        except psycopg2.OperationalError as exc:
            if time.time() >= deadline:
                raise
            reason = (str(exc).strip().splitlines() or ["?"])[0]
            print("[web_service] waiting for the database: " + reason, flush=True)
            time.sleep(2)


def init_db():
    """Wait for the database, then create uploads/, the videos table and its migrations (idempotent)."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    wait_for_db(int(os.environ.get("DB_WAIT_TIMEOUT", "60")))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            cur.execute(SCHEMA)
            for statement in MIGRATIONS:
                cur.execute(statement)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_stem(company_name):
    """'ACME GmbH' -> 'ACME_GmbH' (keeps letters of any language, digits, '.', '-', '_')."""
    stem = re.sub(r"[^\w.\-]+", "_", company_name.strip(), flags=re.UNICODE).strip("._")
    return stem or "video"


def _pick_filename(db, company_name, ext):
    """<safe company name><ext>; if a DIFFERENT company already owns that name
    (e.g. 'A/B' vs 'A_B'), add a short hash so files never overwrite each other."""
    filename = _safe_stem(company_name) + ext
    row = query_one(
        db,
        "SELECT company_name FROM videos WHERE file_path = %s",
        (os.path.join(UPLOAD_DIR, filename),),
    )
    if row is not None and row["company_name"] != company_name:
        digest = hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:8]
        filename = "{}_{}{}".format(_safe_stem(company_name), digest, ext)
    return filename


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _job_dict(row):
    return {k: _jsonable(v) for k, v in row.items()}


def _error(message, status):
    return jsonify({"error": message}), status


def _jsonable(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.route("/upload", methods=["POST"])
def upload_video():
    """UploadVideo: save the file, upsert the DB record, return the video URL."""
    company_name = (request.form.get("company_name") or "").strip()
    if not company_name:
        return _error("company_name is required", 400)

    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return _error("file is required", 400)

    ext = os.path.splitext(uploaded.filename)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,6}", ext):
        ext = ".webm"

    # optional zoom sidecar (<video>.zoom.json written by the recorder)
    zoom_bytes = None
    zoom_upload = request.files.get("zoom")
    if zoom_upload is not None and zoom_upload.filename:
        zoom_bytes = zoom_upload.read(1024 * 1024 + 1)
        if len(zoom_bytes) > 1024 * 1024:
            return _error("zoom file is too large (max 1 MB)", 400)
        try:
            json.loads(zoom_bytes.decode("utf-8-sig"))
        except ValueError:
            return _error("zoom file is not valid JSON", 400)

    db = get_db()
    filename = _pick_filename(db, company_name, ext)
    file_path = os.path.join(UPLOAD_DIR, filename)
    sidecar_path = os.path.join(UPLOAD_DIR, os.path.splitext(filename)[0] + ".zoom.json")

    # a re-upload replaces the record: forget the previously processed video of this company
    old = query_one(db, "SELECT processed_file_path FROM videos WHERE company_name = %s", (company_name,))

    # write to a temp file first, then atomically replace (no half-written videos)
    tmp_path = file_path + ".part"
    uploaded.save(tmp_path)
    os.replace(tmp_path, file_path)

    # the sidecar lives next to the video; without a new sidecar an old one would be stale
    if zoom_bytes is not None:
        with open(sidecar_path + ".part", "wb") as handle:
            handle.write(zoom_bytes)
        os.replace(sidecar_path + ".part", sidecar_path)
    else:
        _remove_quietly(sidecar_path)
    process_status = "pending" if zoom_bytes is not None else "none"   # pending = the worker picks it up

    # build the URL from the host the client actually used (never hard-coded)
    video_url = request.host_url + "media/" + quote(filename)

    # upsert: re-uploading the same company_name updates the row (no duplicates)
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO videos (company_name, file_path, video_url, process_status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (company_name) DO UPDATE SET
                file_path           = EXCLUDED.file_path,
                video_url           = EXCLUDED.video_url,
                updated_at          = NOW(),
                process_status      = EXCLUDED.process_status,
                process_error       = NULL,
                processed_file_path = NULL,
                processed_video_url = NULL,
                processed_at        = NULL
            """,
            (company_name, file_path, video_url, process_status),
        )
    db.commit()
    if old is not None and old["processed_file_path"]:
        _remove_quietly(old["processed_file_path"])

    return jsonify({"company_name": company_name, "video_url": video_url}), 200


@app.route("/get_video_url", methods=["GET"])
def get_video_url():
    """GetVideoURL: look the video up by company name (404 if unknown)."""
    company_name = (request.args.get("company_name") or "").strip()
    if not company_name:
        return _error("company_name is required", 400)

    row = query_one(
        get_db(),
        "SELECT company_name, video_url FROM videos WHERE company_name = %s",
        (company_name,),
    )
    if row is None:
        return _error("company not found: " + company_name, 404)

    return jsonify({"company_name": row["company_name"], "video_url": row["video_url"]}), 200


@app.route("/media/<path:filename>", methods=["GET"])
def media(filename):
    """Serve a stored video. (In Docker Apache serves /media/ directly via Alias;
    this route is the fallback for running the service without Apache.)"""
    return send_from_directory(UPLOAD_DIR, filename, conditional=True)


@app.route("/videos", methods=["GET"])
def list_videos():
    """(optional) list all records."""
    rows = query_all(
        get_db(),
        "SELECT id, company_name, file_path, video_url, created_at, updated_at, "
        "process_status, processed_video_url FROM videos ORDER BY id",
    )
    return jsonify([{k: _jsonable(v) for k, v in row.items()} for row in rows]), 200


@app.route("/jobs", methods=["GET"])
def list_jobs():
    """Zoom processing jobs (one per video); optional ?company_name=X filter."""
    company_name = (request.args.get("company_name") or "").strip()
    if company_name:
        rows = query_all(get_db(), "SELECT " + JOB_COLUMNS + " FROM videos WHERE company_name = %s ORDER BY id",
                         (company_name,))
    else:
        rows = query_all(get_db(), "SELECT " + JOB_COLUMNS + " FROM videos ORDER BY id")
    return jsonify([_job_dict(r) for r in rows]), 200


@app.route("/jobs/<int:video_id>", methods=["GET"])
def get_job(video_id):
    row = query_one(get_db(), "SELECT " + JOB_COLUMNS + " FROM videos WHERE id = %s", (video_id,))
    if row is None:
        return _error("job not found: {}".format(video_id), 404)
    return jsonify(_job_dict(row)), 200


@app.route("/jobs/<int:video_id>/process", methods=["POST"])
def process_job(video_id):
    """Queue the zoom processing; the worker container picks it up (202 Accepted)."""
    db = get_db()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE videos SET
                process_status = 'pending', process_error = NULL,
                processed_file_path = NULL, processed_video_url = NULL, processed_at = NULL
            WHERE id = %s AND process_status NOT IN ('pending', 'processing')
            RETURNING id, company_name, process_status
            """,
            (video_id,),
        )
        queued = cur.fetchone()
    db.commit()
    if queued is not None:
        return jsonify({"id": queued["id"], "company_name": queued["company_name"],
                        "status": queued["process_status"]}), 202

    row = query_one(db, "SELECT process_status FROM videos WHERE id = %s", (video_id,))
    if row is None:
        return _error("job not found: {}".format(video_id), 404)
    return _error("already {}".format(row["process_status"]), 409)


@app.route("/get_processed_video_url", methods=["GET"])
def get_processed_video_url():
    """URL of the zoomed video (404 while it does not exist yet)."""
    company_name = (request.args.get("company_name") or "").strip()
    if not company_name:
        return _error("company_name is required", 400)
    row = query_one(
        get_db(),
        "SELECT company_name, process_status, processed_video_url FROM videos WHERE company_name = %s",
        (company_name,),
    )
    if row is None:
        return _error("company not found: " + company_name, 404)
    if not row["processed_video_url"]:
        return _error("not processed yet (status: {})".format(row["process_status"]), 404)
    return jsonify({"company_name": row["company_name"], "processed_video_url": row["processed_video_url"]}), 200


@app.route("/health", methods=["GET"])
def health():
    """(optional) used by the Docker healthcheck: the web app is up and the DB answers."""
    try:
        query_one(get_db(), "SELECT 1 AS ok")
    except psycopg2.Error:
        return jsonify({"status": "database unavailable"}), 503
    return jsonify({"status": "ok"}), 200


# wait for PostgreSQL, then create uploads/ and the table as soon as the module is loaded
init_db()


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8000"))
    app.run(host=host, port=port, threaded=True)
