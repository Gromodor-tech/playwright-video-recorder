"""
web_service.py - lightweight web service + database layer (Flask + SQLite).

Endpoints:
    POST /upload                         UploadVideo  (multipart: file, company_name)
    GET  /get_video_url?company_name=X   GetVideoURL
    GET  /media/<filename>               serves an uploaded video file
    GET  /videos                         (optional) lists all records

Run:
    python web_service.py
Host / port can be overridden with the FLASK_HOST / FLASK_PORT environment variables
(default 0.0.0.0:8000).

Only flask + the Python standard library are used. The database is a single
SQLite file (videos.db) that stores the company name, the absolute file path and
the video URL - never the video bytes. Files live in ./uploads next to this script.
"""

import hashlib
import os
import re
import sqlite3
from urllib.parse import quote

from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "videos.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,   -- absolute path to the file on disk
    video_url TEXT NOT NULL,   -- public URL to the video
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024    # 500 MB upload limit


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------
def _connect():
    """Open a NEW SQLite connection (one per request - connections are not shared
    between threads). Busy timeout + WAL avoid 'database is locked' on concurrent uploads."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db():
    """Per-request connection stored in flask.g."""
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    """Create uploads/ and videos.db (with the videos table) on first start."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
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
    row = db.execute(
        "SELECT company_name FROM videos WHERE file_path = ?",
        (os.path.join(UPLOAD_DIR, filename),),
    ).fetchone()
    if row is not None and row["company_name"] != company_name:
        digest = hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:8]
        filename = "{}_{}{}".format(_safe_stem(company_name), digest, ext)
    return filename


def _error(message, status):
    return jsonify({"error": message}), status


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

    db = get_db()
    filename = _pick_filename(db, company_name, ext)
    file_path = os.path.join(UPLOAD_DIR, filename)

    # write to a temp file first, then atomically replace (no half-written videos)
    tmp_path = file_path + ".part"
    uploaded.save(tmp_path)
    os.replace(tmp_path, file_path)

    # build the URL from the host the client actually used (never hard-coded)
    video_url = request.host_url + "media/" + quote(filename)

    # upsert: re-uploading the same company_name updates the row (no duplicates)
    db.execute(
        """
        INSERT INTO videos (company_name, file_path, video_url)
        VALUES (?, ?, ?)
        ON CONFLICT(company_name) DO UPDATE SET
            file_path  = excluded.file_path,
            video_url  = excluded.video_url,
            updated_at = datetime('now')
        """,
        (company_name, file_path, video_url),
    )
    db.commit()

    return jsonify({"company_name": company_name, "video_url": video_url}), 200


@app.route("/get_video_url", methods=["GET"])
def get_video_url():
    """GetVideoURL: look the video up by company name (404 if unknown)."""
    company_name = (request.args.get("company_name") or "").strip()
    if not company_name:
        return _error("company_name is required", 400)

    row = get_db().execute(
        "SELECT company_name, video_url FROM videos WHERE company_name = ?",
        (company_name,),
    ).fetchone()
    if row is None:
        return _error("company not found: " + company_name, 404)

    return jsonify({"company_name": row["company_name"], "video_url": row["video_url"]}), 200


@app.route("/media/<path:filename>", methods=["GET"])
def media(filename):
    """Serve a stored video (supports Range requests, so the browser player can seek)."""
    return send_from_directory(UPLOAD_DIR, filename, conditional=True)


@app.route("/videos", methods=["GET"])
def list_videos():
    """(optional) list all records."""
    rows = get_db().execute(
        "SELECT id, company_name, file_path, video_url, created_at, updated_at "
        "FROM videos ORDER BY id"
    ).fetchall()
    return jsonify([dict(r) for r in rows]), 200


# create videos.db and uploads/ as soon as the module is loaded
init_db()


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8000"))
    app.run(host=host, port=port, threaded=True)
