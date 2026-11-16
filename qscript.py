"""
qscript.py - browser automation scenario recorded to video.

Public API:
    PATH_TO_VIDEO_FOLDER  - global, folder for the resulting .webm files
    CaptureVideo_Magento  - the scenario itself (see signature below)

Only third-party dependency: playwright (sync API, Chromium).
Python 3.9+.
"""

import json
import math
import os
import random
import re
import time

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:      # the pure zoom helpers stay importable (unit tests) without playwright
    PlaywrightError = Exception
    sync_playwright = None

# ---------------------------------------------------------------------------
# Global variable (main script may override it at startup)
# ---------------------------------------------------------------------------
PATH_TO_VIDEO_FOLDER = "./video"

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
VIEWPORT = {"width": 1280, "height": 720}

# Set environment variable HEADLESS=0 to see the real browser window
# (helps if DuckDuckGo ever shows a bot check in headless mode).
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

# Zoom effect (Internship Task 2). The zoom itself is a POST effect (apply_zoom.py);
# while recording only the toggle events are captured.
VIDEO_FPS = 25                      # Playwright records videos at 25 fps
ZOOM_FACTOR = 2.0
ZOOM_RAMP_SEC = 0.5
ZOOM_TIME_OFFSET_SEC = 0.0          # calibration: shifts every event time (video start vs. t0)
# the sample scenario triggers a few zooms through window.__zoomAt (ZOOM_DEMO=0 turns it off)
ENABLE_ZOOM_DEMO = os.environ.get("ZOOM_DEMO", "1") != "0"

# Current DuckDuckGo selectors
SEARCH_BOX = 'textarea[name="q"]'
SEARCH_BUTTON = 'form button[type="submit"]'
FIRST_RESULT = 'a[data-testid="result-title-a"], li[data-layout="organic"] h2 a'

# ---------------------------------------------------------------------------
# JavaScript injected into every page (add_init_script, wrapped in an IIFE).
#
# It draws, inside the page (so that it is captured by the video):
#   * a white SVG arrow cursor with a black outline (~32x32, pointer-events: none)
#   * a semi-transparent yellow circle (radius 32px) behind the arrow
#   * a ripple on every mousedown
#   * window.ShowCallout(text, bgColor, textColor, fade, hold)
# The last pointer position is kept in sessionStorage, so after a full page
# navigation (DuckDuckGo search) the cursor reappears where it was.
# ---------------------------------------------------------------------------
OVERLAY_JS = r"""
(() => {
  if (window.self !== window.top) return;          // only the top frame
  if (window.__vcInstalled) return;
  window.__vcInstalled = true;

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const STORE_KEY = '__vc_pos';
  let pos = { x: 24, y: 24 };
  try {
    const saved = sessionStorage.getItem(STORE_KEY);
    if (saved) {
      const p = JSON.parse(saved);
      if (typeof p.x === 'number' && typeof p.y === 'number') pos = p;
    }
  } catch (e) {}

  let root = null, hl = null, cur = null;

  const place = () => {
    if (!hl || !cur) return;
    hl.style.transform = 'translate(' + (pos.x - 32) + 'px,' + (pos.y - 32) + 'px)';
    cur.style.transform = 'translate(' + (pos.x - 4) + 'px,' + (pos.y - 2) + 'px)';
  };

  const getRoot = () => {
    const de = document.documentElement;
    if (!de) return null;
    const existing = document.getElementById('__vc_root');
    if (existing) return existing;

    root = document.createElement('div');
    root.id = '__vc_root';
    root.style.cssText =
      'position:fixed;left:0;top:0;width:0;height:0;overflow:visible;' +
      'z-index:2147483647;pointer-events:none;';

    // yellow highlight (behind the arrow)
    hl = document.createElement('div');
    hl.style.cssText =
      'position:fixed;left:0;top:0;width:64px;height:64px;border-radius:50%;' +
      'background:rgba(255,235,0,0.35);pointer-events:none;will-change:transform;';

    // white SVG arrow with black outline, 32x32
    cur = document.createElementNS(SVG_NS, 'svg');
    cur.setAttribute('width', '32');
    cur.setAttribute('height', '32');
    cur.setAttribute('viewBox', '0 0 32 32');
    cur.style.cssText =
      'position:fixed;left:0;top:0;pointer-events:none;overflow:visible;will-change:transform;';
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', 'M4 2 L4 25 L10 19.5 L14.5 29 L19 27 L14.5 17.8 L22.5 17.8 Z');
    path.setAttribute('fill', '#ffffff');
    path.setAttribute('stroke', '#000000');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('stroke-linejoin', 'round');
    cur.appendChild(path);

    root.appendChild(hl);
    root.appendChild(cur);
    de.appendChild(root);
    place();
    return root;
  };

  const ripple = (x, y) => {
    const r0 = getRoot();
    if (!r0) return;
    const size = 36;
    const r = document.createElement('div');
    r.style.cssText =
      'position:fixed;left:' + (x - size / 2) + 'px;top:' + (y - size / 2) + 'px;' +
      'width:' + size + 'px;height:' + size + 'px;border-radius:50%;box-sizing:border-box;' +
      'border:3px solid rgba(255,120,0,0.95);background:rgba(255,120,0,0.15);' +
      'pointer-events:none;';
    r0.insertBefore(r, cur);                       // keep the arrow on top
    const a = r.animate(
      [{ transform: 'scale(0.1)', opacity: 0.95 }, { transform: 'scale(1)', opacity: 0 }],
      { duration: 650, easing: 'ease-out' }
    );
    a.onfinish = () => r.remove();
  };

  window.addEventListener('mousemove', (e) => {
    pos = { x: e.clientX, y: e.clientY };
    getRoot();
    place();
    try { sessionStorage.setItem(STORE_KEY, JSON.stringify(pos)); } catch (err) {}
  }, true);

  window.addEventListener('mousedown', (e) => ripple(e.clientX, e.clientY), true);

  // Callout: yellow rectangle, black 16pt text, auto-sized, fade in / hold / fade out.
  window.ShowCallout = function (text, bgColor, textColor, fade, hold) {
    const r0 = getRoot();
    if (!r0) return;
    fade = (typeof fade === 'number') ? fade : 2000;
    hold = (typeof hold === 'number') ? hold : 2000;
    const old = document.getElementById('__vc_callout');
    if (old) old.remove();

    const el = document.createElement('div');
    el.id = '__vc_callout';
    el.textContent = text;
    const s = el.style;
    s.position = 'fixed';
    s.left = '50%';
    s.top = '110px';
    s.transform = 'translateX(-50%)';
    s.padding = '8px 18px';
    s.background = bgColor || '#ffeb3b';
    s.color = textColor || '#000000';
    s.fontSize = '16pt';
    s.fontFamily = 'Arial, Helvetica, sans-serif';
    s.whiteSpace = 'nowrap';
    s.borderRadius = '4px';
    s.boxShadow = '0 2px 8px rgba(0,0,0,0.3)';
    s.pointerEvents = 'none';
    s.opacity = '0';
    r0.insertBefore(el, hl);

    const fin = el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: fade, fill: 'forwards' });
    fin.onfinish = () => {
      setTimeout(() => {
        const fout = el.animate([{ opacity: 1 }, { opacity: 0 }], { duration: fade, fill: 'forwards' });
        fout.onfinish = () => el.remove();
      }, hold);
    };
  };

  // ----- Zoom toggle: F5 hotkey and window.__zoomAt(x, y) ----------------------------------
  // The first call is the start of a zoom-in, the next one the start of the zoom-out, and so on.
  // The state lives in Python (window.__zoomEmit is exposed by Playwright for the whole context,
  // so it survives page navigations); Python answers with {type: 'in' | 'out'}.
  const zoomRing = (x, y, kind) => {
    const r0 = getRoot();
    if (!r0) return;
    const size = 90;
    const color = kind === 'in' ? 'rgba(0,200,83,0.95)' : 'rgba(255,23,68,0.95)';   // green in, red out
    const ring = document.createElement('div');
    ring.style.cssText =
      'position:fixed;left:' + (x - size / 2) + 'px;top:' + (y - size / 2) + 'px;' +
      'width:' + size + 'px;height:' + size + 'px;border-radius:50%;box-sizing:border-box;' +
      'border:5px solid ' + color + ';pointer-events:none;';
    r0.insertBefore(ring, cur);
    const from = kind === 'in' ? 'scale(1.7)' : 'scale(0.5)';
    const a = ring.animate(
      [{ transform: from, opacity: 0.95 }, { transform: 'scale(1)', opacity: 0 }],
      { duration: 900, easing: 'ease-out' }
    );
    a.onfinish = () => ring.remove();
  };

  window.__zoomAt = async function (x, y) {
    if (typeof x !== 'number' || typeof y !== 'number') { x = pos.x; y = pos.y; }   // current mouse
    let kind = 'in';
    try {
      if (typeof window.__zoomEmit === 'function') {
        const res = await window.__zoomEmit(Date.now(), Math.round(x), Math.round(y));
        if (res && res.type) kind = res.type;
      }
    } catch (e) {}
    zoomRing(x, y, kind);
    return kind;
  };

  window.addEventListener('keydown', (e) => {
    if (e.key !== 'F5' || e.repeat) return;
    e.preventDefault();                 // do not reload the page
    e.stopPropagation();
    window.__zoomAt();
  }, true);

  // Mount as soon as <html> exists and re-mount if the page ever removes the overlay.
  getRoot();
  document.addEventListener('DOMContentLoaded', getRoot);
  setInterval(getRoot, 300);
})();
"""


# ---------------------------------------------------------------------------
# Helpers (Python side)
# ---------------------------------------------------------------------------
def show_callout(page, text, bgColor="#ffeb3b", textColor="#000000", fade=2000):
    """Python wrapper for the in-page ShowCallout(text, bgColor, textColor, fade).

    Does not block: the callout fades in, stays and fades out on its own.
    """
    page.evaluate(
        "([t, b, c, f]) => window.ShowCallout(t, b, c, f)",
        [text, bgColor, textColor, fade],
    )


def move_cursor(page, state, x, y):
    """Glide the real mouse from the current position to (x, y).

    Uses smoothstep easing and a slight curve, ~60 events per second,
    so the cursor never teleports.
    """
    x0, y0 = state["x"], state["y"]
    dist = math.hypot(x - x0, y - y0)
    if dist < 1:
        return

    duration_ms = max(450, min(1500, dist * 1.7))
    steps = max(12, int(duration_ms / 16))

    # perpendicular unit vector -> gentle arc instead of a ruler-straight line
    nx, ny = -(y - y0) / dist, (x - x0) / dist
    amplitude = min(40.0, dist * 0.08) * random.choice((-1, 1))

    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)                    # smoothstep ease-in-out
        bend = math.sin(math.pi * t) * amplitude
        px = x0 + (x - x0) * e + nx * bend
        py = y0 + (y - y0) * e + ny * bend
        page.mouse.move(px, py)
        page.wait_for_timeout(16)

    page.mouse.move(x, y)
    state["x"], state["y"] = x, y


def click_here(page):
    """Click at the current mouse position (the page draws the ripple)."""
    page.mouse.down()                               # ripple starts on mousedown
    page.wait_for_timeout(random.randint(300, 380)) # hold so the ripple is visible even if the click navigates
    page.mouse.up()


def type_humanlike(page, text):
    """Type text symbol by symbol with human-like random delays."""
    for ch in text:
        page.keyboard.type(ch)
        delay = random.uniform(70, 190)
        if ch == " ":
            delay += random.uniform(40, 120)
        if random.random() < 0.08:                 # occasional "thinking" pause
            delay += random.uniform(150, 320)
        page.wait_for_timeout(delay)


def wait_for_box(page, selector, timeout_ms=45000):
    """Poll until the element is visible and has a box; return the box dict.

    Results are rendered client-side and can take 10+ seconds, and a full
    navigation may happen meanwhile, so errors are swallowed and retried.
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            loc = page.locator(selector)
            for i in range(loc.count()):
                item = loc.nth(i)
                if not item.is_visible():           # skip hidden duplicates
                    continue
                box = item.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    return box
        except PlaywrightError:
            pass                                    # navigation in progress / not rendered yet
        page.wait_for_timeout(300)
    raise RuntimeError("Element did not appear in time: " + selector)


def _safe_filename(name):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (name or "").strip())
    return cleaned or "video"


# ---------------------------------------------------------------------------
# Zoom events (Internship Task 2)
# ---------------------------------------------------------------------------
def sidecar_path_for(video_path):
    """ACME_GmbH.webm -> ACME_GmbH.zoom.json (next to the video)."""
    return os.path.splitext(video_path)[0] + ".zoom.json"


class ZoomRecorder(object):
    """Collects zoom toggle events {t, x, y, type} with t = seconds since the video start.

    window.__zoomEmit (see OVERLAY_JS) calls on_toggle() for every F5 press or
    window.__zoomAt() call. The first toggle is "in", the next one "out", and so on.
    The timestamp comes from the browser (Date.now()), so the time the Python callback
    happens to run does not matter. t = 0 is the moment right after the page (= the video)
    was created; ZOOM_TIME_OFFSET_SEC lets you calibrate a constant shift.
    """

    def __init__(self, viewport=None, fps=VIDEO_FPS, zoom_factor=ZOOM_FACTOR,
                 ramp_sec=ZOOM_RAMP_SEC, time_offset=0.0, clock=time.time):
        self.viewport = dict(viewport or VIEWPORT)
        self.fps = fps
        self.zoom_factor = zoom_factor
        self.ramp_sec = ramp_sec
        self.time_offset = time_offset
        self._clock = clock
        self._t0 = None
        self.zoomed = False
        self.events = []

    def start(self, t0=None):
        """Mark t = 0 (call right after the page was created)."""
        self._t0 = self._clock() if t0 is None else t0

    def on_toggle(self, ts_ms, x, y):
        """Callback for window.__zoomEmit(Date.now(), x, y); returns {"type": "in" | "out"}."""
        if self._t0 is None:
            self.start()
        t = float(ts_ms) / 1000.0 - self._t0 + self.time_offset
        kind = "out" if self.zoomed else "in"
        self.zoomed = not self.zoomed
        self.events.append({
            "t": round(max(0.0, t), 3),
            "x": int(round(float(x or 0))),
            "y": int(round(float(y or 0))),
            "type": kind,
        })
        return {"type": kind}

    def sidecar_dict(self):
        return {
            "viewport": {"width": self.viewport["width"], "height": self.viewport["height"]},
            "fps": self.fps,
            "zoom_factor": self.zoom_factor,
            "ramp_sec": self.ramp_sec,
            "events": list(self.events),
        }

    def write_sidecar(self, video_path):
        """Write <video>.zoom.json - ONLY when at least one event exists (normal recordings
        are unaffected). Without events a stale sidecar from an earlier run is removed.
        Returns the sidecar path or None."""
        path = sidecar_path_for(video_path)
        if not self.events:
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        part = path + ".part"
        with open(part, "w", encoding="utf-8") as handle:
            json.dump(self.sidecar_dict(), handle, indent=2)
            handle.write("\n")
        os.replace(part, path)
        return path


def zoom_toggle(page, state, wait=True):
    """Trigger the same toggle as the F5 key at the current mouse position (scenario helper).

    Returns "in" / "out" (None when the zoom demo is switched off). With wait=True the
    scenario pauses for one ramp so the zoom animation in the final video is finished before
    the next action starts.
    """
    if not ENABLE_ZOOM_DEMO:
        return None
    kind = page.evaluate("([x, y]) => window.__zoomAt(x, y)", [state["x"], state["y"]])
    if wait:
        page.wait_for_timeout(int(ZOOM_RAMP_SEC * 1000) + 150)
    return kind


def zoom_pulse(page, state, hold_ms):
    """Zoom in at the mouse position, hold, zoom out (two toggles)."""
    if not ENABLE_ZOOM_DEMO:
        return
    zoom_toggle(page, state)
    page.wait_for_timeout(hold_ms)
    zoom_toggle(page, state)


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------
def _run_scenario(page, url, city):
    state = {"x": 24.0, "y": 24.0}
    query = "Magento developer in " + city

    # 1. open the page
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.locator(SEARCH_BOX).first.wait_for(state="visible", timeout=30000)

    # 2. cursor is injected by the init script; sync the real mouse with it
    page.mouse.move(state["x"], state["y"])
    page.wait_for_timeout(700)

    # callout (fades in and out on its own, does not block)
    show_callout(page, "Searching: " + query)

    # 3. glide to the search box (one detour first, like a human looking around)
    move_cursor(page, state, 760, 380)
    page.wait_for_timeout(250)
    box = wait_for_box(page, SEARCH_BOX, 30000)
    move_cursor(page, state, box["x"] + box["width"] * 0.35, box["y"] + box["height"] / 2)
    click_here(page)
    page.wait_for_timeout(400)

    # zoom in on the search box while typing (1st toggle = zoom-in)
    zoom_toggle(page, state)

    # 4. type the query symbol by symbol
    type_humanlike(page, query)
    page.wait_for_timeout(700)

    # zoom back out (2nd toggle = zoom-out)
    zoom_toggle(page, state)

    # 5. click the search button
    pressed_enter = False
    try:
        box = wait_for_box(page, SEARCH_BUTTON, 8000)
        move_cursor(page, state, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        click_here(page)
    except RuntimeError:
        page.keyboard.press("Enter")                # fallback: button not found
        pressed_enter = True

    # 6. wait for results (full page navigation happens here; polling handles it)
    try:
        wait_for_box(page, FIRST_RESULT, 12000)
    except RuntimeError:
        if not pressed_enter:
            page.keyboard.press("Enter")            # fallback if the click did not submit
        wait_for_box(page, FIRST_RESULT, 45000)
    page.wait_for_timeout(1200)

    # 7. move to the first result and click it
    box = wait_for_box(page, FIRST_RESULT, 15000)
    target_x = box["x"] + min(box["width"] * 0.4, 220)
    target_y = box["y"] + min(box["height"] / 2, 14)
    move_cursor(page, state, target_x, target_y)
    page.wait_for_timeout(300)
    zoom_pulse(page, state, hold_ms=1200)           # zoom in, hold, zoom out
    click_here(page)

    # let the clicked site load so the video shows the result of the click
    try:
        page.wait_for_url(
            lambda u: "duckduckgo.com" not in u,
            timeout=20000,
            wait_until="domcontentloaded",
        )
    except PlaywrightError:
        pass
    page.wait_for_timeout(3000)


def CaptureVideo_Magento(
    url,
    company_name,
    language,
    country,
    postcode,
    city,
    street,
    house_no,
):
    """Record a video of the sample scenario.

    All parameters are strings from one CSV row. Returns the absolute path to
    the resulting .webm file, or None on failure.
    (language, country, postcode, street, house_no are accepted for the
    exact signature required by the task; the sample scenario only needs
    url, company_name and city.)
    """
    out_dir = os.path.abspath(PATH_TO_VIDEO_FOLDER)
    os.makedirs(out_dir, exist_ok=True)
    final_path = os.path.join(out_dir, _safe_filename(company_name) + ".webm")

    if sync_playwright is None:
        raise RuntimeError("playwright is not installed: pip install -r requirements.txt")

    tmp_path = None
    zoom = None
    try:
        with sync_playwright() as pw:
            launch_kwargs = dict(
                headless=HEADLESS,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                # full Chromium (new headless mode) looks less like a bot
                browser = pw.chromium.launch(channel="chromium", **launch_kwargs)
            except PlaywrightError:
                browser = pw.chromium.launch(**launch_kwargs)
            try:
                # normal Chrome user agent (default headless UA contains "HeadlessChrome")
                user_agent = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/{} Safari/537.36".format(browser.version)
                )
                context = browser.new_context(
                    user_agent=user_agent,
                    locale="en-US",
                    viewport=VIEWPORT,
                    record_video_dir=out_dir,
                    record_video_size=VIEWPORT,
                )
                context.add_init_script(script=OVERLAY_JS)
                zoom = ZoomRecorder(VIEWPORT, VIDEO_FPS, ZOOM_FACTOR, ZOOM_RAMP_SEC,
                                    time_offset=ZOOM_TIME_OFFSET_SEC)
                context.expose_function("__zoomEmit", zoom.on_toggle)   # F5 / __zoomAt -> events
                page = context.new_page()
                zoom.start()                        # t = 0: the video starts with the page
                video = page.video

                scenario_error = None
                try:
                    _run_scenario(page, url, city)
                except Exception as exc:            # keep going: we must close the context
                    scenario_error = exc
                    try:                            # debug info: what did the page show?
                        shot = os.path.join(out_dir, "debug_" + _safe_filename(company_name) + ".png")
                        page.screenshot(path=shot)
                        print("[qscript] page url: {} | title: {} | screenshot: {}".format(
                            page.url, page.title(), shot))
                    except Exception:
                        pass
                finally:
                    context.close()                 # finalizes the video file

                tmp_path = video.path() if video is not None else None
                if scenario_error is not None:
                    raise scenario_error
            finally:
                browser.close()

        if not tmp_path or not os.path.exists(tmp_path):
            raise RuntimeError("Playwright did not produce a video file")

        os.replace(tmp_path, final_path)            # rename/move to <company_name>.webm
        zoom.write_sidecar(final_path)              # <company_name>.zoom.json, only if there are events
        return final_path

    except Exception as exc:
        print("[qscript] FAILED for '{}': {}".format(company_name, exc))
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return None
