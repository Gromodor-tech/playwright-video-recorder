"""
check_f5.py - manual check of the F5 zoom hotkey in a REAL Chromium window.

    python check_f5.py

A visible Chromium opens for 25 seconds. Move the mouse over the page and press F5:
  1st F5 -> green ring (zoom in), 2nd F5 -> red ring (zoom out), and so on.
The page must NOT reload. Every press is printed here; at the end the recorded events
(the same ones that go into <video>.zoom.json) are printed. Do not close the window.
"""

import qscript

SECONDS = 25


def main():
    if qscript.sync_playwright is None:
        raise SystemExit("playwright is not installed: pip install -r requirements.txt")

    zoom = qscript.ZoomRecorder(qscript.VIEWPORT, qscript.VIDEO_FPS,
                                qscript.ZOOM_FACTOR, qscript.ZOOM_RAMP_SEC)

    def on_toggle(ts_ms, x, y):                      # called by window.__zoomEmit (F5 / __zoomAt)
        result = zoom.on_toggle(ts_ms, x, y)
        print("  F5 -> {} at ({}, {})".format(result["type"], int(x), int(y)), flush=True)
        return result

    with qscript.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            context = browser.new_context(viewport=qscript.VIEWPORT)
            context.add_init_script(script=qscript.OVERLAY_JS)
            context.expose_function("__zoomEmit", on_toggle)
            page = context.new_page()
            zoom.start()
            page.goto("https://duckduckgo.com", wait_until="domcontentloaded")
            print("Move the mouse over the page and press F5 (zoom in), then F5 again (zoom out).")
            print("The window stays open for {} seconds - do not close it.".format(SECONDS))
            page.wait_for_timeout(SECONDS * 1000)
        finally:
            browser.close()

    print("\nRecorded events:")
    for event in zoom.events:
        print("  ", event)
    if not zoom.events:
        print("   (none - F5 was not detected: click on the page once and try again)")


if __name__ == "__main__":
    main()
