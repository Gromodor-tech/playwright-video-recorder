# Video Recorder, Zoom Post-Processing & Company Enrichment Service

Comprehensive solution for automated browser video recording with Camtasia-style mouse zoom effects, background video processing via FFmpeg, and CSV-based company & Impressum data enrichment backed by PostgreSQL and Docker.

## Core Components

* **Recorder (`capture_video.py`, `qscript.py`)**: Records browser sessions per CSV row using Python and Playwright, captures zoom events (via `F5` hotkey or programmatic `window.__zoomAt`), and uploads videos alongside `.zoom.json` sidecars.
* **Web Service (`web_service.py`)**: Flask application served under Apache + `mod_wsgi`. Handles video uploads, URL lookups by company name, media file serving, company records, and zoom job management.
* **Company & Impressum Enrichment (`enrich_companies.py`, `impressum.py`, `db.py`, `schema.sql`)**: Imports and enriches company records from CSV datasets (`input.csv`, `companies.csv`), parses *Impressum* pages for legal/contact details, and persists structured records in PostgreSQL (with optional LLM extraction via DeepSeek API).
* **Zoom Post-Processing (`apply_zoom.py`)**: Uses FFmpeg `zoompan` filters with smoothstep easing to render smooth zoom-in, hold, and zoom-out transitions centered on cursor coordinates.
* **Background Worker (`worker.py`)**: Dedicated container polling PostgreSQL (`FOR UPDATE SKIP LOCKED`) to process pending zoom jobs asynchronously and update database statuses.

## Architecture

```
 recorder (host)  --upload video + <video>.zoom.json-->  host:8000
                                                            |
                                 [ web container ]  Apache :80 + mod_wsgi + Flask
                                     |  Apache serves /media/ directly (Alias), the rest goes to Flask
                                     |  status 'pending'
                                     v
 [ db container ]  postgres:16  <---- polls ----  [ worker container ]  python worker.py + ffmpeg
   volume db_data                                        |
                                                         v
            volume "uploads" (/app/uploads, shared by web and worker):
            ACME.webm, ACME.zoom.json (sidecar), ACME.zoomed.webm (result)
```

`web`, `worker`, and `enrich` share the same Docker image (`Dockerfile` with FFmpeg pre-installed); `worker` and `enrich` override the container entrypoint to run their respective scripts.

## Project Structure

| File / Directory | Purpose |
| --- | --- |
| `qscript.py` | Playwright recording scenario & zoom capture (`F5` hotkey, `window.__zoomAt`, visual indicator ring, `<video>.zoom.json` generator) |
| `apply_zoom.py` | Zoom math & FFmpeg post-processing CLI (`--video`, `--dry-run`, `--ffmpeg`, `--supersample`, `--crf`) |
| `check_f5.py` | Standalone manual check for the `F5` zoom toggle in a visible Chromium window (25s session) |
| `enrich_companies.py` | CSV company data import and enrichment pipeline |
| `impressum.py` | Impressum page discovery and legal/contact data extraction |
| `db.py`, `schema.sql` | PostgreSQL connection utilities and database schema initialization |
| `input.csv`, `companies.csv` | Input and enriched CSV company datasets |
| `worker.py` | Background job queue consumer (`FOR UPDATE SKIP LOCKED`) running FFmpeg zoom rendering |
| `web_service.py` | Flask REST API, PostgreSQL persistence layer, and zoom job endpoints |
| `video_client.py` | Python HTTP client (`UploadVideo`, `GetVideoURL`, `GetJob`, `ProcessVideo`, `WaitForProcessed`) |
| `capture_video.py` | Batch CSV loop for recording, uploading, and previewing processed videos |
| `Dockerfile`, `docker-compose.yml` | Multi-container stack (`web`, `worker`, `db`, and `enrich` under `tools` profile) with `uploads` and `db_data` volumes |
| `app.wsgi`, `apache-video.conf`, `docker-entrypoint.sh` | Apache + `mod_wsgi` configuration and container startup script |
| `.env.example`, `.gitignore`, `.dockerignore` | Environment configuration template and ignore rules (`.env` is excluded from Git) |
| `tests/`, `pytest.ini`, `requirements-dev.txt` | Unit and integration test suite |
| `.github/workflows/tests.yml` | GitHub Actions CI pipeline running `pytest` on Python `3.12` |

## Setup & Running the Stack

### Prerequisites

* Docker and Docker Compose (Docker Desktop on Windows/macOS)
* Python 3.12+ (if running the host recorder or enrichment scripts locally outside Docker)

### 1. Start Docker Containers (Works Out of the Box)

The stack runs immediately **without creating a `.env` file**—`docker-compose.yml` provides built-in defaults (`DB_NAME=videos`, `DB_USER=video_user`, `DB_PASSWORD=video_pass`) across `db`, `web`, `worker`, and `enrich`.

```powershell
docker compose up -d --build
```

* **Web Service URL:** `http://127.0.0.1:8000`
* **Health Check:** `GET http://127.0.0.1:8000/health` -> `{"status": "ok"}`
* **Check Container Status:** `docker compose ps` (`web`, `worker`, and `db` running)
* **Follow Worker Logs:** `docker compose logs -f worker`

### 2. Optional Environment Overrides (`.env`)

Create a `.env` file only if you need to provide `DEEPSEEK_API_KEY` for LLM-assisted enrichment or override default database credentials:

```powershell
cp .env.example .env
```

Supported variables in `docker-compose.yml`:

* `DB_NAME` (default: `videos`)
* `DB_USER` (default: `video_user`)
* `DB_PASSWORD` (default: `video_pass`)
* `DEEPSEEK_API_KEY` (optional; empty by default)
* `DEEPSEEK_MODEL` (default: `deepseek-chat`)

> **Important Notes on `.env` & Database Volumes:**
> 1. Never commit `.env` to Git (it is already ignored in `.gitignore`).
> 2. PostgreSQL initializes `POSTGRES_USER` and `POSTGRES_PASSWORD` only when the `db_data` volume is first created. If you add or change `DB_USER` / `DB_PASSWORD` in `.env` **after** already starting the stack once, reset the volume first to avoid authentication mismatches:
>    ```powershell
>    docker compose down -v
>    docker compose up -d --build
>    ```
> 3. Existing database schemas are migrated automatically on startup (`ADD COLUMN IF NOT EXISTS`).

To stop the stack while keeping volumes intact:

```powershell
docker compose down
```

## Company & Impressum Enrichment

The `enrich` service is configured under the `tools` Docker Compose profile so it runs on demand without blocking the main `web` / `worker` services:

* **`schema.sql` & `db.py`**: Define and manage PostgreSQL tables and connection lifecycle.
* **`impressum.py`**: Discovers and extracts legal/contact information from company *Impressum* pages.
* **`enrich_companies.py`**: Reads CSV records (`input.csv` / `companies.csv`), runs enrichment, and stores results in PostgreSQL.

Run enrichment inside Docker (passes all environment variables, including `DEEPSEEK_API_KEY` if set):

```powershell
docker compose --profile tools run --rm enrich
```

## Zoom Recording & Post-Processing

### 1. Event Capture (`qscript.py`)

Zoom is applied as a **post-processing effect**—only timestamped toggle events are saved during recording:

* **`F5` Hotkey**: Toggles zoom at the current cursor position (`in` on first press, `out` on second press) without reloading the page. Requires a visible browser window (`HEADLESS=0`).
* **`window.__zoomAt(x, y)`**: Programmatic trigger used inside Playwright scenarios (`zoom_toggle()` / `zoom_pulse()`). Set `ZOOM_DEMO=0` to disable demo zooms.
* **Visual Feedback**: Displays a brief colored ring at the target coordinates (**green** = zoom in, **red** = zoom out).
* **1:1 Coordinate Mapping**: `record_video_size` matches `viewport` (`1280x720`), ensuring cursor coordinates map directly to video pixels.
* **Sidecar Output**: `<video>.zoom.json` is generated next to the video **only if at least one zoom event occurred**:

```json
{
  "viewport": {"width": 1280, "height": 720},
  "fps": 25,
  "zoom_factor": 2.0,
  "ramp_sec": 0.5,
  "events": [
    {"t": 3.0, "x": 640, "y": 360, "type": "in"},
    {"t": 8.0, "x": 640, "y": 360, "type": "out"}
  ]
}
```

Use `ZOOM_TIME_OFFSET_SEC` in `qscript.py` if minor start-time calibration is needed.

### 2. FFmpeg Post-Processing (`apply_zoom.py`)

```powershell
python apply_zoom.py --video video/ACME_GmbH.webm              # -> video/ACME_GmbH.zoomed.webm
python apply_zoom.py --video video/ACME_GmbH.webm --dry-run    # Print FFmpeg command without encoding
python apply_zoom.py --video video/ACME_GmbH.webm --ffmpeg "C:\tools\ffmpeg\bin\ffmpeg.exe"
```

Or run inside the Docker worker container:

```powershell
docker compose run --rm --entrypoint python worker apply_zoom.py --video /app/uploads/ACME_GmbH.webm
```

**Processing Rules:**

* Pairs every `in` event with the following `out` event (holding zoom between timestamps); an unpaired trailing `in` holds until the end of the video.
* Uses smoothstep easing (`ease-in` / `ease-out`) calculated per frame.
* Clamps the `iw/zoom x ih/zoom` crop window within frame boundaries.
* Upscales input 2x (`--supersample`) prior to cropping to prevent integer-pixel jitter during slow zooms, outputting a VP9 `.webm` at the original resolution.
* Copies the input file unchanged if no sidecar or events exist.

## API Endpoints & Worker Workflow

1. Recorder uploads the video and optional `<video>.zoom.json` sidecar via `POST /upload`.
2. Web service saves both files to `/app/uploads` and sets `process_status = 'pending'`.
3. `worker` locks the pending row (`FOR UPDATE SKIP LOCKED`), renders `<name>.zoomed.webm`, and updates PostgreSQL (`process_status = 'done'`, `processed_video_url`, `processed_file_path`, `processed_at`, or `failed` with `process_error`). Interrupted jobs are re-queued on worker startup.

**Job Status Lifecycle:** `none` (no sidecar) -> `pending` -> `processing` -> `done` | `failed`

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/upload` | Multipart upload (`file`, `company_name`, optional `zoom` sidecar); returns `{"company_name", "video_url"}` |
| `GET` | `/get_video_url?company_name=X` | Returns original video URL (`404` if not found) |
| `GET` | `/get_processed_video_url?company_name=X` | Returns zoomed video URL (`404` until processing completes) |
| `GET` | `/jobs` / `/jobs/<id>` | Lists zoom jobs (supports `?company_name=X`) or fetches a single job |
| `POST` | `/jobs/<id>/process` | Queues or re-queues video processing (`202` accepted, `409` if already active) |
| `GET` | `/media/<file>`, `/videos`, `/health` | Direct media delivery via Apache, video listing, and healthcheck |

## Recording & Uploading Videos

Install host dependencies and Playwright Chromium:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
python capture_video.py --server http://127.0.0.1:8000 --wait-processed 300
```

* `--wait-processed SECONDS`: Waits for the worker to finish zoom rendering before opening the zoomed video preview.
* `--view-seconds N`: Duration to keep the preview window open (default: `5`; `0` waits for `Enter`).
* `HEADLESS=0`: Runs Chromium visibly (recommended to avoid DuckDuckGo bot checks and to use `F5` manual zoom).

**Manual cURL Test:**

```bash
curl -F "file=@video/Test Company Minsk.webm" \
     -F "zoom=@video/Test Company Minsk.zoom.json" \
     -F "company_name=Test Company Minsk" \
     http://127.0.0.1:8000/upload

curl "http://127.0.0.1:8000/jobs?company_name=Test%20Company%20Minsk"
curl "http://127.0.0.1:8000/get_processed_video_url?company_name=Test%20Company%20Minsk"
```

## Testing & CI

Run the test suite locally:

```powershell
pip install -r requirements-dev.txt
python -m pytest -v
```

* **`tests/test_apply_zoom.py`**: Tests zoom math, smoothstep ramps, boundary clamping, sidecar parsing, dry-run output, and FFmpeg rendering verification.
* **`tests/test_zoom_capture.py`**: Tests `ZoomRecorder`, conditional sidecar writing, and `__zoomAt`/`F5` event handling.
* **`tests/test_docker_stack.py`**: End-to-end Docker integration test (enabled via `RUN_DOCKER_TESTS=1 python -m pytest tests/test_docker_stack.py -v`).

**Continuous Integration:** `.github/workflows/tests.yml` automatically runs `pytest` on `ubuntu-latest` using **Python 3.12** on every `push` and `pull_request`.

## Notes

* `mod_wsgi` in `requirements.txt` compiles against Apache development headers inside the Docker image and is skipped on Windows. On Linux/macOS hosts without Apache headers, remove `mod_wsgi` if installing locally only for the recorder.