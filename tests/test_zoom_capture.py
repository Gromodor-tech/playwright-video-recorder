"""Tests for the zoom capture part of qscript.py (events, sidecar file, F5 / __zoomAt hooks).

No browser is started: ZoomRecorder is plain Python, the JavaScript is checked with node
(if installed) against a tiny DOM stub.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

import apply_zoom as az
import qscript


# ---------------------------------------------------------------------------
# ZoomRecorder: events
# ---------------------------------------------------------------------------
def recorder(**kwargs):
    rec = qscript.ZoomRecorder(clock=lambda: 1000.0, **kwargs)
    rec.start()                                      # t = 0 is wall-clock second 1000
    return rec


def test_first_toggle_is_zoom_in_second_is_zoom_out():
    rec = recorder()
    assert rec.on_toggle(1003000, 640, 360) == {"type": "in"}
    assert rec.on_toggle(1008000, 700, 400) == {"type": "out"}
    assert rec.on_toggle(1012000, 100, 100) == {"type": "in"}
    assert [e["type"] for e in rec.events] == ["in", "out", "in"]


def test_event_format_and_times_relative_to_the_video_start():
    rec = recorder()
    rec.on_toggle(1003000, 640, 360)
    rec.on_toggle(1008500, 640.4, 359.6)
    assert rec.events[0] == {"t": 3.0, "x": 640, "y": 360, "type": "in"}
    assert rec.events[1] == {"t": 8.5, "x": 640, "y": 360, "type": "out"}   # px are integers


def test_time_is_never_negative_and_offset_is_applied():
    rec = recorder()
    rec.on_toggle(999000, 1, 1)                      # before the start
    assert rec.events[0]["t"] == 0.0
    shifted = recorder(time_offset=0.25)
    shifted.on_toggle(1002000, 1, 1)
    assert shifted.events[0]["t"] == 2.25


# ---------------------------------------------------------------------------
# sidecar file
# ---------------------------------------------------------------------------
def test_sidecar_is_not_written_without_events(tmp_path):
    video = str(tmp_path / "ACME_GmbH.webm")
    assert recorder().write_sidecar(video) is None
    assert os.listdir(str(tmp_path)) == []           # normal recordings are unaffected


def test_stale_sidecar_of_an_earlier_recording_is_removed(tmp_path):
    video = str(tmp_path / "ACME_GmbH.webm")
    stale = tmp_path / "ACME_GmbH.zoom.json"
    stale.write_text("{}")
    assert recorder().write_sidecar(video) is None
    assert not stale.exists()


def test_sidecar_is_written_next_to_the_video_in_the_spec_format(tmp_path):
    video = str(tmp_path / "ACME_GmbH.webm")
    rec = recorder()
    rec.on_toggle(1003000, 640, 360)
    rec.on_toggle(1008000, 640, 360)
    path = rec.write_sidecar(video)

    assert path == str(tmp_path / "ACME_GmbH.zoom.json") == qscript.sidecar_path_for(video)
    data = json.loads(open(path, encoding="utf-8").read())
    assert data == {
        "viewport": {"width": 1280, "height": 720},
        "fps": 25,
        "zoom_factor": 2.0,
        "ramp_sec": 0.5,
        "events": [{"t": 3.0, "x": 640, "y": 360, "type": "in"},
                   {"t": 8.0, "x": 640, "y": 360, "type": "out"}],
    }
    assert not os.path.exists(path + ".part")


def test_sidecar_written_by_the_recorder_is_understood_by_apply_zoom(tmp_path):
    rec = recorder()
    rec.on_toggle(1002000, 300, 200)
    rec.on_toggle(1006000, 300, 200)
    rec.on_toggle(1010000, 900, 500)                 # open-ended
    path = rec.write_sidecar(str(tmp_path / "v.webm"))
    sc = az.load_sidecar(path)
    segs = az.build_segments(sc.events, sc.fps, sc.ramp_sec, sc.viewport)
    assert len(segs) == 2 and segs[0].down_start is not None and segs[1].down_start is None


# ---------------------------------------------------------------------------
# scenario helpers (fake page)
# ---------------------------------------------------------------------------
class FakePage(object):
    def __init__(self, answers=("in", "out")):
        self.evaluated = []
        self.waits = []
        self._answers = list(answers)

    def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))
        return self._answers.pop(0) if self._answers else "in"

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def test_zoom_toggle_calls_window_zoomAt_with_the_mouse_position(monkeypatch):
    monkeypatch.setattr(qscript, "ENABLE_ZOOM_DEMO", True)
    page = FakePage()
    assert qscript.zoom_toggle(page, {"x": 12.5, "y": 34.0}) == "in"
    script, arg = page.evaluated[0]
    assert "window.__zoomAt" in script and arg == [12.5, 34.0]
    assert page.waits == [int(qscript.ZOOM_RAMP_SEC * 1000) + 150]      # waits for the ramp


def test_zoom_pulse_is_in_hold_out(monkeypatch):
    monkeypatch.setattr(qscript, "ENABLE_ZOOM_DEMO", True)
    page = FakePage()
    qscript.zoom_pulse(page, {"x": 1, "y": 2}, hold_ms=1200)
    assert len(page.evaluated) == 2 and 1200 in page.waits


def test_zoom_demo_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(qscript, "ENABLE_ZOOM_DEMO", False)
    page = FakePage()
    assert qscript.zoom_toggle(page, {"x": 1, "y": 2}) is None
    qscript.zoom_pulse(page, {"x": 1, "y": 2}, hold_ms=500)
    assert page.evaluated == [] and page.waits == []


# ---------------------------------------------------------------------------
# JavaScript: F5 hotkey and window.__zoomAt
# ---------------------------------------------------------------------------
def test_overlay_js_defines_the_zoom_hooks():
    js = qscript.OVERLAY_JS
    assert "window.__zoomAt" in js and "__zoomEmit" in js
    assert "'F5'" in js and "preventDefault" in js          # F5 must not reload the page
    assert "e.repeat" in js                                   # holding the key = one toggle


JS_HARNESS = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const handlers = {};
const el = () => ({ style: {}, children: [], setAttribute() {}, appendChild(c) { this.children.push(c); },
                    insertBefore(c) { this.children.push(c); }, remove() {},
                    animate() { const a = {}; setTimeout(() => a.onfinish && a.onfinish(), 0); return a; } });
global.window = global; global.self = global; global.top = global;
global.document = { documentElement: el(), getElementById: () => null, createElement: el,
                    createElementNS: () => el(), addEventListener() {} };
global.sessionStorage = { getItem: () => null, setItem() {} };
global.window.addEventListener = (name, fn) => { handlers[name] = fn; };
global.setInterval = () => 0;
const calls = []; let next = 'in';
global.__zoomEmit = async (ts, x, y) => { calls.push([typeof ts, x, y]); const t = next; next = next === 'in' ? 'out' : 'in'; return { type: t }; };
eval(code);
const sleep = () => new Promise(r => setTimeout(r, 5));
(async () => {
  let prevented = 0;
  const key = (k, repeat) => ({ key: k, repeat: !!repeat, preventDefault() { prevented++; }, stopPropagation() {} });
  handlers.mousemove({ clientX: 321, clientY: 123 });
  handlers.keydown(key('F5'));        await sleep();
  handlers.keydown(key('F5', true));  await sleep();      // auto-repeat: ignored
  handlers.keydown(key('F6'));        await sleep();      // other key: ignored
  handlers.keydown(key('F5'));        await sleep();
  const kind = await window.__zoomAt(50.4, 60.6);
  console.log(JSON.stringify({ calls, prevented, kind }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_f5_and_zoomAt_behave_as_specified_in_a_dom_stub(tmp_path):
    match = re.search(r'OVERLAY_JS = r"""(.*?)"""', open(qscript.__file__, encoding="utf-8").read(), re.S)
    (tmp_path / "overlay.js").write_text(match.group(1), encoding="utf-8")
    (tmp_path / "harness.js").write_text(JS_HARNESS, encoding="utf-8")

    check = subprocess.run(["node", "--check", str(tmp_path / "overlay.js")], stderr=subprocess.PIPE)
    assert check.returncode == 0, check.stderr
    run = subprocess.run(["node", str(tmp_path / "harness.js"), str(tmp_path / "overlay.js")],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=True)
    result = json.loads(run.stdout.strip().splitlines()[-1])

    # two real F5 presses (repeat and F6 ignored) at the current mouse position + the programmatic call
    assert result["calls"] == [["number", 321, 123], ["number", 321, 123], ["number", 50, 61]]
    assert result["prevented"] == 2                          # F5 default action (reload) cancelled
    assert result["kind"] == "in"                            # 3rd toggle of the stub: in/out/in
