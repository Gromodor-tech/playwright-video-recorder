# CSV-driven Browser Automation Video Recorder

Records a video of automated browser actions (Python + Playwright, Chromium) for every row of an input CSV file.

## Files

- `capture_video.py` - main script: reads the CSV and calls `qscript.CaptureVideo_Magento(...)` for every row.
- `qscript.py` - the automation function `CaptureVideo_Magento(...)` and the global `PATH_TO_VIDEO_FOLDER`.
- `input.csv` - sample input (UTF-8, delimiter `;`, qualifier `"`).
- `requirements.txt` - dependencies (only `playwright`).
- `video/` - an example of a recorded video.

## Run

```
pip install -r requirements.txt
python -m playwright install chromium
python capture_video.py
```

Optional arguments: `python capture_video.py [csv_path] [video_folder]`.
Videos are saved as `<video_folder>/<Company Name>.webm`, and the path is printed for every row.

To watch the browser live: `HEADLESS=0 python capture_video.py` (PowerShell: `$env:HEADLESS=0; python capture_video.py`).

## What the scenario does

1. Opens the page (DuckDuckGo in the sample).
2. Shows a custom SVG cursor (white arrow, black outline, 32x32) with a semi-transparent yellow highlight circle.
3. Glides the cursor to the search box and clicks it (click ripple).
4. Types `Magento developer in <city>` symbol by symbol with human-like delays.
5. Clicks the search button, waits for the results (full page navigation is handled on the Python side).
6. Moves the cursor to the first result and clicks it.

A callout (`window.ShowCallout(text, bgColor, textColor, fade)`) is shown at the beginning.
