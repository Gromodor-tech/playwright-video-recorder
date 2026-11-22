"""
worker.py - background worker that applies the zoom effect to uploaded videos.

It runs in its own container (same image as the web service, see docker-compose.yml), polls
PostgreSQL for videos with process_status = 'pending', runs apply_zoom.py (ffmpeg) on them and
stores the result:

    pending -> processing -> done   (processed_file_path, processed_video_url, processed_at)
                          -> failed (process_error)

Several workers can run at the same time: a job is claimed with
`FOR UPDATE SKIP LOCKED`, so no video is processed twice.

Environment: the same DB_* variables as the web service, plus (optional)
    FFMPEG               path to the ffmpeg binary (default: ffmpeg)
    WORKER_POLL_SECONDS  pause between polls when the queue is empty (default 2)
"""

import os
import sys
import time
import traceback
from urllib.parse import quote

import psycopg2.extras

import apply_zoom
import web_service as ws        # importing it waits for PostgreSQL and creates/migrates the table

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "2"))


def log(message):
    print("[worker] " + message, flush=True)


def processed_url_for(video_url, filename):
    """The processed file lives next to the original, so reuse the original's URL prefix
    (it already contains the host the client used when it uploaded the video)."""
    return video_url.rsplit("/", 1)[0] + "/" + quote(filename)


def requeue_stuck_jobs(conn):
    """A worker that crashed (or was stopped) while processing leaves 'processing' rows behind."""
    with conn.cursor() as cur:
        cur.execute("UPDATE videos SET process_status = 'pending' WHERE process_status = 'processing'")
        count = cur.rowcount
    conn.commit()
    if count:
        log("re-queued {} unfinished job(s)".format(count))


def claim_job(conn):
    """Atomically take the oldest pending job (None if the queue is empty)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE videos SET process_status = 'processing', process_error = NULL
            WHERE id = (
                SELECT id FROM videos
                WHERE process_status = 'pending'
                ORDER BY id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, company_name, file_path, video_url
            """
        )
        job = cur.fetchone()
    conn.commit()
    return job


def finish_job(conn, job_id, processed_path, processed_url):
    # `AND process_status = 'processing'`: if the video was re-uploaded meanwhile, do not
    # attach the result of the OLD video to the new record
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE videos SET process_status = 'done', process_error = NULL,
                processed_file_path = %s, processed_video_url = %s, processed_at = NOW()
            WHERE id = %s AND process_status = 'processing'
            """,
            (processed_path, processed_url, job_id),
        )
        updated = cur.rowcount
    conn.commit()
    return updated == 1


def fail_job(conn, job_id, message):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE videos SET process_status = 'failed', process_error = %s "
            "WHERE id = %s AND process_status = 'processing'",
            (message[:2000], job_id),
        )
    conn.commit()


def process_job(conn, job):
    video_path = job["file_path"]
    output_path = os.path.splitext(video_path)[0] + ".zoomed.webm"
    log("job {} ({}): {}".format(job["id"], job["company_name"], video_path))
    started = time.time()
    try:
        result = apply_zoom.process_video(video_path, output_path, ffmpeg=FFMPEG, log=log)
    except Exception as exc:                        # a broken video must never stop the worker
        log("job {} failed: {}".format(job["id"], exc))
        fail_job(conn, job["id"], str(exc))
        return
    url = processed_url_for(job["video_url"], os.path.basename(output_path))
    if finish_job(conn, job["id"], output_path, url):
        log("job {} done in {:.1f}s ({}): {}".format(job["id"], time.time() - started, result.mode, url))
    else:
        log("job {} was replaced while processing - result dropped".format(job["id"]))


def process_one():
    """Claim and process one job. Returns True if there was a job."""
    conn = ws._connect()
    try:
        job = claim_job(conn)
        if job is None:
            return False
        process_job(conn, job)
        return True
    finally:
        conn.close()


def main():
    log("started (ffmpeg: {}, poll every {}s)".format(FFMPEG, POLL_SECONDS))
    conn = ws._connect()
    try:
        requeue_stuck_jobs(conn)
    finally:
        conn.close()
    while True:
        try:
            worked = process_one()
        except Exception:                           # e.g. the database restarted: log and retry
            traceback.print_exc()
            sys.stderr.flush()
            worked = False
        if not worked:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
