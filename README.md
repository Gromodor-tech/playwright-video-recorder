# CSV-driven Browser Automation Video Recorder + Video Web Service

**Task 1** - records a video of automated browser actions (Python + Playwright, Chromium) for every row of an input CSV file.
**Task 2** - a small web service (Flask + SQLite) to upload, store, look up and preview those videos.

## Files

- `capture_video.py` - main script: reads the CSV, records a video per row; with `--server` also uploads it and previews it in Chromium.
- `qscript.py` - the automation function `CaptureVideo_Magento(...)` and the global `PATH_TO_VIDEO_FOLDER`.
- `web_service.py` - Flask web service + SQLite database layer.
- `video_client.py` - client functions `UploadVideo(...)` and `GetVideoURL(...)` (uses `requests`, imported lazily).
- `input.csv` - sample input (UTF-8, delimiter `;`, qualifier `"`).
- `requirements.txt` - `playwright`, `flask`, `requests`.
- `video/` - an example of a recorded video.

## Install

```
pip install -r requirements.txt
python -m playwright install chromium
```

## Task 1: record videos

```
python capture_video.py
```

Optional arguments: `python capture_video.py [csv_path] [video_folder]`.
Videos are saved as `<video_folder>/<Company Name>.webm`, the path is printed for every row.
To watch the browser live: `HEADLESS=0 python capture_video.py` (PowerShell: `$env:HEADLESS=0; python capture_video.py`).

## Task 2: web service, database, upload + preview

Start the service (creates `videos.db` and `uploads/` on first start):

```
python web_service.py
```

It listens on `0.0.0.0:8000` by default; override with `FLASK_HOST` / `FLASK_PORT`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/upload` | UploadVideo: multipart `file` + `company_name`, returns `{"company_name", "video_url"}`; re-upload of the same company updates the record (upsert) |
| GET | `/get_video_url?company_name=X` | GetVideoURL: returns `{"company_name", "video_url"}`, `404` if unknown |
| GET | `/media/<filename>` | serves the stored video (playable in a browser, supports Range requests) |
| GET | `/videos` | (optional) lists all records |

Database: SQLite `videos.db`, table `videos(id, company_name UNIQUE, file_path, video_url, created_at, updated_at)`.
Only the path and the URL are stored, never the video bytes. A new connection is opened per request, WAL mode and a busy timeout are enabled.
The URL is built from `request.host_url`, nothing is hard-coded.

Record, upload and preview (in a second terminal, while the service is running):

```
python capture_video.py --server http://127.0.0.1:8000
python capture_video.py --server http://127.0.0.1:8000 --view-seconds 10   # keep the preview 10 s
python capture_video.py --server http://127.0.0.1:8000 --view-seconds 0    # keep it open until Enter
```

After each recorded video the script calls `UploadVideo(...)`, logs the returned URL and opens it in Chromium (`headless=False`).
A failed upload or preview is logged and the batch continues with the next row.

Quick manual check without the recorder (curl):

```
curl -F "file=@video/Test Company Minsk.webm" -F "company_name=Test Company Minsk" http://127.0.0.1:8000/upload
curl "http://127.0.0.1:8000/get_video_url?company_name=Test%20Company%20Minsk"
```

## What the Task 1 scenario does

1. Opens the page (DuckDuckGo in the sample).
2. Shows a custom SVG cursor (white arrow, black outline, 32x32) with a semi-transparent yellow highlight circle.
3. Glides the cursor to the search box and clicks it (click ripple).
4. Types `Magento developer in <city>` symbol by symbol with human-like delays.
5. Clicks the search button, waits for the results (full page navigation is handled on the Python side).
6. Moves the cursor to the first result and clicks it.

A callout (`window.ShowCallout(text, bgColor, textColor, fade)`) is shown at the beginning.
