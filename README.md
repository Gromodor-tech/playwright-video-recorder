# Video recorder + web service (Apache/mod_wsgi + PostgreSQL in Docker)

- **Recorder** (`capture_video.py`, `qscript.py`) - records a browser video per CSV row (Python + Playwright) and can upload it to the web service.
- **Web service** (`web_service.py`) - Flask app: upload a video, look up its URL by company name, serve the file.
- **Internship Task 1**: the web service runs under **Apache + mod_wsgi** in one container, the database is **PostgreSQL** in another one, both started with **Docker Compose**.

## Architecture

```
 host:8000 ──► [ web container ]  Apache :80 + mod_wsgi + Flask (web_service.py)
                    │   │  Apache serves /media/ directly (Alias), everything else -> Flask
                    │   └── volume "uploads"  -> /app/uploads   (video files)
                    ▼
               [ db container ]  postgres:16
                    └── volume "db_data" -> /var/lib/postgresql/data
```

| File | Purpose |
|---|---|
| `Dockerfile` | builds the Apache + mod_wsgi + Flask image (mod_wsgi is built by pip, registered with `mod_wsgi-express module-config`) |
| `docker-compose.yml` | services `web` and `db`, volumes `uploads` and `db_data`, healthchecks, `depends_on: service_healthy` |
| `web_service.py` | Flask service, migrated from SQLite to PostgreSQL (psycopg2) |
| `app.wsgi` | WSGI entry point (`from web_service import app as application`) |
| `apache-video.conf` | Apache VirtualHost + WSGI configuration |
| `docker-entrypoint.sh` | starts Apache in the foreground |
| `requirements.txt` | flask, psycopg2-binary, mod_wsgi (skipped on Windows), playwright, requests |
| `.env.example` | DB settings template (copy to `.env`) |

## Run the web service + database

Requires Docker with Docker Compose (Docker Desktop on Windows/macOS).

```
docker compose up --build
```

The service is then available at http://127.0.0.1:8000 (Apache port 80 inside the container is mapped to 8000).
The `web` container starts only after PostgreSQL is healthy, and the service additionally retries the database connection at startup, so it does not crash if the database is still starting.

Optional: change the database credentials (do not commit the real `.env`):

```
copy .env.example .env      # Windows   (Linux/macOS: cp .env.example .env)
# edit .env, then: docker compose up --build
```

Without `.env` the development defaults from `docker-compose.yml` are used. If you change the password after the database volume was created, run `docker compose down -v` first (this deletes the data).

Stop: `docker compose down` (data and videos are kept in the volumes). `docker compose down -v` also deletes the volumes.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/upload` | UploadVideo: multipart `file` + `company_name`, returns `{"company_name", "video_url"}`; the same company is updated (upsert) |
| GET | `/get_video_url?company_name=X` | GetVideoURL: returns `{"company_name", "video_url"}`, `404` if unknown |
| GET | `/media/<filename>` | the stored video (served by Apache, playable in a browser) |
| GET | `/videos` | (optional) list of records |
| GET | `/health` | (optional) web + database check, used by the Docker healthcheck |

Database settings are passed only through environment variables: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.
Table `videos(id SERIAL, company_name UNIQUE, file_path, video_url, created_at, updated_at)`; only the path and the URL are stored, never the video bytes.

## Quick check

```
curl -F "file=@video/Test Company Minsk.webm" -F "company_name=Test Company Minsk" http://127.0.0.1:8000/upload
curl "http://127.0.0.1:8000/get_video_url?company_name=Test%20Company%20Minsk"
curl http://127.0.0.1:8000/videos
```

Persistence check: `docker compose down`, then `docker compose up`, then `curl http://127.0.0.1:8000/videos` - the records are still there and the video URL still plays.

Look inside a container: `docker compose exec web curl http://localhost/health`, logs: `docker compose logs web`.

## Recorder (runs on the host, not in Docker)

```
pip install -r requirements.txt
python -m playwright install chromium
python capture_video.py                                   # record videos to ./video
python capture_video.py --server http://127.0.0.1:8000    # + upload and preview in Chromium
python capture_video.py --server http://127.0.0.1:8000 --view-seconds 0   # keep the preview open until Enter
```

Input: `input.csv` (UTF-8, delimiter `;`, qualifier `"`). `--view-seconds N` keeps the preview open N seconds (default 5), `0` waits for Enter. A failed upload or preview is logged and the batch continues.
To watch the recording browser live: `HEADLESS=0 python capture_video.py` (PowerShell: `$env:HEADLESS=0; python capture_video.py`).

Note: `mod_wsgi` in `requirements.txt` is only needed inside the Docker image. On Linux/macOS without Apache development headers it cannot be built locally - remove that line for a local recorder-only install.
