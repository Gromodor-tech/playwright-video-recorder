"""
capture_video.py - reads input.csv and records one video per row.

Usage:
    python capture_video.py                      # input.csv -> ./video
    python capture_video.py my.csv               # custom CSV
    python capture_video.py my.csv ./my_videos   # custom output folder
"""

import csv
import sys

import qscript


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "input.csv"
    if len(sys.argv) > 2:
        qscript.PATH_TO_VIDEO_FOLDER = sys.argv[2]   # override the global at startup

    total = 0
    saved = 0

    # utf-8-sig = plain UTF-8, but also tolerates a BOM written by Excel
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        for number, row in enumerate(reader, start=1):
            total += 1
            print("[{}] {} ...".format(number, row.get("Company Name", "")))

            path = qscript.CaptureVideo_Magento(
                url=row.get("URL", ""),
                company_name=row.get("Company Name", ""),
                language=row.get("Language", ""),
                country=row.get("Country", ""),
                postcode=row.get("Postcode", ""),
                city=row.get("City", ""),
                street=row.get("Street", ""),
                house_no=row.get("HouseNo", ""),
            )

            if path:
                saved += 1
                print("    saved: " + path)
            else:
                print("    FAILED")

    print("Done: {}/{} videos saved.".format(saved, total))


if __name__ == "__main__":
    main()
