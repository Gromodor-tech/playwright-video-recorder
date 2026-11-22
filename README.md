# Video recorder + web service + Camtasia-style zoom (Docker)

- **Recorder** (`capture_video.py`, `qscript.py`) - records a browser video per CSV row (Python + Playwright), can trigger a **zoom** (F5 or `window.__zoomAt`) and uploads the video.
- **Web service** (`web_service.py`) - Flask app under Apache + mod_wsgi: upload a video, look up its URL by company name, serve the files, manage zoom jobs.
- **Zoom post-processing** (`apply_zoom.py`) - ffmpeg `zoompan`: smooth zoom-in -> hold -> zoom-out around the mouse position.
- **Worker** (`worker.py`) - separate container that applies the zoom to uploaded videos and stores the result in PostgreSQL.

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

web and worker are built from the same image (`Dockerfile`, ffmpeg installed), the worker only runs another command.

| File | Purpose |
|---|---|
| `qscript.py` | recording scenario + zoom capture: F5 hotkey, `window.__zoomAt`, indicator ring, `<video>.zoom.json` |
| `apply_zoom.py` | zoom math + ffmpeg post-processing (CLI: `--video`, `--dry-run`, `--ffmpeg`) |
| `check_f5.py` | manual check of the F5 hotkey in a real Chromium window (25 s, prints every toggle) |
| `worker.py` | background worker (job queue in PostgreSQL, `FOR UPDATE SKIP LOCKED`) |
| `web_service.py` | Flask service, PostgreSQL layer, zoom job endpoints |
| `video_client.py` | `UploadVideo`, `GetVideoURL`, `GetJob`, `ProcessVideo`, `WaitForProcessed` |
| `capture_video.py` | CSV loop: record, upload, preview |
| `Dockerfile`, `docker-compose.yml` | `web`, `worker`, `db`; volumes `uploads`, `db_data` |
| `app.wsgi`, `apache-video.conf`, `docker-entrypoint.sh` | Apache + mod_wsgi setup |
| `tests/`, `pytest.ini`, `requirements-dev.txt`, `.github/workflows/tests.yml` | pytest suite and CI |

## Run the stack

Requires Docker with Docker Compose (Docker Desktop on Windows/macOS).

```
docker compose up -d --build
```

Service: http://127.0.0.1:8000 (`/health` -> `{"status":"ok"}`). Check: `docker compose ps` (web, worker, db running), logs: `docker compose logs -f worker`.

Database credentials come from environment variables (`DB_NAME`, `DB_USER`, `DB_PASSWORD`). To change the defaults copy `.env.example` to `.env` (never commit it). If you change them after the database volume was created run `docker compose down -v` first (deletes the data).
Stop: `docker compose down` (data and videos stay in the volumes).

An existing database from the previous version is upgraded automatically (the new columns are added with `ADD COLUMN IF NOT EXISTS`).

## Zoom - how it works

### 1. Capture (qscript.py)

The zoom is a **post effect**: while recording only the toggle events are saved.

- **F5** toggles the zoom at the current mouse position: the first press marks the start of the zoom-in, the second press the start of the zoom-out (and so on). F5 does not reload the page. A key is used by a human operator, so run the recorder with a visible browser: `HEADLESS=0` (PowerShell: `$env:HEADLESS=0; python capture_video.py`).
- **`window.__zoomAt(x, y)`** triggers the same toggle from the scenario (`await window.__zoomAt(x, y)`; without arguments the current mouse position is used). The sample scenario uses it through `zoom_toggle()` / `zoom_pulse()`; `ZOOM_DEMO=0` switches the demo zooms off.
- A colored ring appears at the point for a moment: **green = zoom in, red = zoom out**.
- The state lives in Python (`ZoomRecorder`, exposed to every page with `context.expose_function`), so it survives page navigations such as the DuckDuckGo search. The event time is the browser clock (`Date.now()`) minus the moment the page (= the video) was created, so `t` is "seconds since video start".
- Mouse pixels are video pixels 1:1 because `record_video_size == viewport` (1280x720).
- `<video>.zoom.json` is written next to the video **only if at least one event exists** (a stale sidecar of an earlier run is removed):

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

Quick manual check of the hotkey without recording a video: `python check_f5.py` (a visible Chromium opens for 25 s, press F5 twice: green ring = zoom in, red ring = zoom out; every press is printed).

If the zoom in the final video starts slightly early or late, shift all events with `ZOOM_TIME_OFFSET_SEC` in `qscript.py` (calibration of "video start" vs. "page created").

### 2. Post-processing (apply_zoom.py)

```
python apply_zoom.py --video video/ACME_GmbH.webm              # -> video/ACME_GmbH.zoomed.webm
python apply_zoom.py --video video/ACME_GmbH.webm --dry-run    # print the ffmpeg command, encode nothing
python apply_zoom.py --video video/ACME_GmbH.webm --ffmpeg "C:\tools\ffmpeg\bin\ffmpeg.exe"
```

Options: `--output`, `--sidecar`, `--zoom-factor`, `--ramp-sec`, `--supersample`, `--crf`. Without ffmpeg on the PATH use `--ffmpeg <path>` (Windows: `winget install Gyan.FFmpeg`) - or run it inside the stack: `docker compose run --rm --entrypoint python worker apply_zoom.py --video /app/uploads/ACME_GmbH.webm`.

Rules:
- every `in` is paired with the next `out`: ramp-in starts at the `in` press, ramp-out at the `out` press, **the hold is the time between the two presses**; multiple non-overlapping pairs are supported (a zoom never starts before the previous one is finished);
- an `in` without a following `out` holds the zoom to the end of the video;
- the ramps are eased (smoothstep: ease-in and ease-out, not linear); everything is calculated in frames;
- the crop is `iw/zoom x ih/zoom`, centered on the mouse point and clamped to the frame: `x = clamp(px - iw/(2*zoom), 0, iw - iw/zoom)`, same for `y`;
- old sidecars without `type` are read as a toggle sequence (in, out, in, ...); no sidecar or no events -> the video is copied unchanged;
- the output is VP9 `.webm` with the input resolution. The input is up-scaled 2x inside the filter (`--supersample`) because `zoompan` crops at whole pixels, which would make a slow zoom shake.

### 3. Docker integration

1. The recorder uploads the video **and** its sidecar (`UploadVideo` sends `<video>.zoom.json` automatically if it exists).
2. `POST /upload` stores the sidecar next to the video and sets `process_status = 'pending'`.
3. The `worker` container claims the job (`FOR UPDATE SKIP LOCKED`), runs `apply_zoom.py` (ffmpeg is installed in the image), writes `<name>.zoomed.webm` into the shared `uploads` volume and stores the result in PostgreSQL: `process_status = 'done'`, `processed_video_url`, `processed_file_path`, `processed_at` (on an error `failed` + `process_error`). A job that was interrupted by a restart is queued again at startup.

Status: `none` (uploaded without a sidecar) -> `pending` -> `processing` -> `done` | `failed`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/upload` | multipart `file` + `company_name` + optional `zoom` (the sidecar); response unchanged: `{"company_name", "video_url"}` |
| GET | `/get_video_url?company_name=X` | original video URL (404 if unknown) |
| GET | `/get_processed_video_url?company_name=X` | zoomed video URL (404 until it is ready) |
| GET | `/jobs[?company_name=X]`, `/jobs/<id>` | job status, `processed_video_url` |
| POST | `/jobs/<id>/process` | (re)queue the processing of a video (202, 409 if already pending/processing; a video without a sidecar is copied unchanged) |
| GET | `/media/<file>`, `/videos`, `/health` | files (served by Apache), all records, healthcheck |

Table `videos`: `id, company_name (UNIQUE), file_path, video_url, created_at, updated_at` + `process_status, process_error, processed_file_path, processed_video_url, processed_at`.
Only paths and URLs are stored, never video bytes. A re-upload of a company replaces the record and resets the result of the previous version.

## Record, upload and watch the zoomed video

```
pip install -r requirements.txt
python -m playwright install chromium
python capture_video.py --server http://127.0.0.1:8000 --wait-processed 300
```

For every CSV row: record (the sample scenario zooms into the search box while typing and into the first result), upload video + sidecar, wait until the worker finished (`--wait-processed SECONDS`) and open the **zoomed** video in Chromium. Without `--wait-processed` the original video is previewed and the job is processed in the background (`GET /jobs`). `--view-seconds N` keeps the preview open N seconds (default 5; `0` = until Enter).

Manual check with curl:

```
curl -F "file=@video/Test Company Minsk.webm" -F "zoom=@video/Test Company Minsk.zoom.json" -F "company_name=Test Company Minsk" http://127.0.0.1:8000/upload
curl "http://127.0.0.1:8000/jobs?company_name=Test%20Company%20Minsk"
curl "http://127.0.0.1:8000/get_processed_video_url?company_name=Test%20Company%20Minsk"
```

## Tests

```
pip install -r requirements-dev.txt
python -m pytest -v
```

- `tests/test_apply_zoom.py` - zoom math without ffmpeg: ramp frames and easing, clamping, multiple in/out pairs, open-ended zoom, sidecar parsing, old sidecars without `type`, "no events -> copy unchanged", dry-run, and that the generated ffmpeg expressions equal the Python model. With ffmpeg installed (skipped otherwise) it renders a real video and checks that the zoomed frames equal the expected crop (centered on the point, clamped in the corner, resolution preserved).
- `tests/test_zoom_capture.py` - `ZoomRecorder`, sidecar writing (only with events), `__zoomAt`/F5 logic (node, DOM stub).
- `tests/test_docker_stack.py` - integration: ffmpeg is installed in `web` and `worker`, an uploaded video is processed and the result is stored in PostgreSQL. Needs the running stack: `RUN_DOCKER_TESTS=1 python -m pytest tests/test_docker_stack.py -v` (otherwise skipped).

CI: `.github/workflows/tests.yml` runs the suite on Python 3.9 and 3.12 for every push.

## Notes

- `mod_wsgi` in `requirements.txt` is built inside the Docker image (needs Apache headers) and skipped on Windows. On Linux/macOS without Apache development headers remove that line for a local recorder-only install.
- To watch the recording browser live: `HEADLESS=0`. DuckDuckGo may show a bot check in headless mode.
