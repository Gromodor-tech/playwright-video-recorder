"""
capture_video.py - reads input.csv and records one video per row.
Optionally uploads every recorded video to the web service and previews it in Chromium.

Usage:
    python capture_video.py                                   # input.csv -> ./video
    python capture_video.py my.csv ./my_videos                # custom CSV / output folder
    python capture_video.py --server http://127.0.0.1:8000    # + upload and preview
    python capture_video.py --server http://127.0.0.1:8000 --view-seconds 0
                                                              # keep the preview open until Enter
"""

import argparse
import csv

import qscript


def preview_video(url, view_seconds):
    """Open the uploaded video in a visible Chromium window.

    view_seconds > 0 : keep the browser open that many seconds, then close it.
    view_seconds == 0: keep it open until the user presses Enter.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url)                                  # built-in media player of Chromium
            try:                                            # start playback if it did not autoplay
                page.evaluate("() => { const v = document.querySelector('video'); if (v) v.play(); }")
            except Exception:
                pass
            if view_seconds > 0:
                page.wait_for_timeout(view_seconds * 1000)
            else:
                input("Preview is open. Press Enter to close the browser... ")
        finally:
            browser.close()


def upload_and_preview(server_url, video_path, company_name, view_seconds):
    """Upload one video, log the returned URL, preview it. Raises on failure."""
    from video_client import UploadVideo                    # lazy: requests is only needed here

    video_url = UploadVideo(server_url, video_path, company_name)
    print("    uploaded: " + video_url)
    preview_video(video_url, view_seconds)


def parse_args():
    parser = argparse.ArgumentParser(description="Record a video per CSV row (optionally upload + preview).")
    parser.add_argument("csv_path", nargs="?", default="input.csv", help="input CSV file (default: input.csv)")
    parser.add_argument("video_folder", nargs="?", default=None, help="output folder for videos (default: ./video)")
    parser.add_argument("--server", default=None,
                        help="web service URL, e.g. http://127.0.0.1:8000 - enables upload + preview")
    parser.add_argument("--view-seconds", type=float, default=5,
                        help="preview time in seconds; 0 = keep the browser open until Enter (default: 5)")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.video_folder:
        qscript.PATH_TO_VIDEO_FOLDER = args.video_folder    # override the global at startup

    total = 0
    saved = 0
    uploaded = 0

    # utf-8-sig = plain UTF-8, but also tolerates a BOM written by Excel
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        for number, row in enumerate(reader, start=1):
            total += 1
            company = row.get("Company Name", "")
            print("[{}] {} ...".format(number, company))

            path = qscript.CaptureVideo_Magento(
                url=row.get("URL", ""),
                company_name=company,
                language=row.get("Language", ""),
                country=row.get("Country", ""),
                postcode=row.get("Postcode", ""),
                city=row.get("City", ""),
                street=row.get("Street", ""),
                house_no=row.get("HouseNo", ""),
            )

            if not path:
                print("    FAILED")
                continue

            saved += 1
            print("    saved: " + path)

            if args.server:
                try:
                    upload_and_preview(args.server, path, company, args.view_seconds)
                    uploaded += 1
                except Exception as exc:                    # never stop the batch loop
                    print("    UPLOAD/PREVIEW FAILED: {}".format(exc))

    summary = "Done: {}/{} videos saved.".format(saved, total)
    if args.server:
        summary += " {} uploaded.".format(uploaded)
    print(summary)


if __name__ == "__main__":
    main()
