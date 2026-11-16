"""Tests for apply_zoom.py: zoom math, ffmpeg filter generation, sidecar parsing, processing paths.

Everything except the last test group runs WITHOUT ffmpeg.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

import apply_zoom as az

SIZE = (1280, 720)
VP = (1280, 720)
FPS = 25.0


def event(t, x=640, y=360, kind=None):
    ev = {"t": t, "x": x, "y": y}
    if kind:
        ev["type"] = kind
    return ev


def eval_ffmpeg(expr, **variables):
    """Evaluate an ffmpeg expression (only if/lt/clip + arithmetic are used) with Python."""
    py = re.sub(r"\bif\(", "_if(", expr)
    py = re.sub(r"\blt\(", "_lt(", py)
    py = re.sub(r"\bclip\(", "_clip(", py)
    env = {
        "_if": lambda cond, a, b: a if cond else b,
        "_lt": lambda a, b: 1.0 if a < b else 0.0,
        "_clip": lambda v, lo, hi: max(lo, min(hi, v)),
    }
    env.update(variables)
    return eval(py, {"__builtins__": {}}, env)


# ---------------------------------------------------------------------------
# easing / frame arithmetic
# ---------------------------------------------------------------------------
def test_ease_has_ease_in_and_ease_out():
    assert az.ease_in_out(0) == 0
    assert az.ease_in_out(1) == 1
    assert az.ease_in_out(0.5) == pytest.approx(0.5)
    assert az.ease_in_out(0.05) < 0.05          # slow start
    assert az.ease_in_out(0.95) > 0.95          # slow end
    values = [az.ease_in_out(i / 100.0) for i in range(101)]
    assert values == sorted(values)             # monotonic


def test_ease_is_clamped_outside_0_1():
    assert az.ease_in_out(-3) == 0
    assert az.ease_in_out(7) == 1


def test_ramp_frames():
    assert az.ramp_frames(0.4, 25) == 10
    assert az.ramp_frames(1.0, 25) == 25
    assert az.ramp_frames(0, 25) == 1           # never 0: no division by zero in the expressions
    assert az.ramp_frames(0.001, 25) == 1


def test_time_to_frame():
    assert az.time_to_frame(3.0, 25) == 75
    assert az.time_to_frame(3.019, 25) == 75    # nearest frame
    assert az.time_to_frame(3.03, 25) == 76
    assert az.time_to_frame(-1, 25) == 0


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------
def test_normalize_sorts_and_keeps_types():
    events = [event(8, kind="out"), event(3, kind="in")]
    result = az.normalize_events(events, VP)
    assert [(e.t, e.type) for e in result] == [(3.0, "in"), (8.0, "out")]


def test_normalize_clamps_coordinates_and_skips_invalid_entries():
    events = [
        {"t": 1, "x": -50, "y": 99999, "type": "in"},
        {"x": 5, "y": 5, "type": "out"},            # no t -> skipped
        {"t": "abc", "type": "out"},                # not a number -> skipped
        "garbage",
        {"t": 4, "type": "out"},                    # no coordinates -> frame centre
    ]
    result = az.normalize_events(events, VP)
    assert len(result) == 2
    assert (result[0].x, result[0].y) == (0, 720)
    assert (result[1].x, result[1].y) == (640, 360)


def test_old_sidecar_without_type_is_a_toggle_sequence():
    events = [event(1), event(2), event(5), event(6), event(9)]
    result = az.normalize_events(events, VP)
    assert [e.type for e in result] == ["in", "out", "in", "out", "in"]


def test_duplicate_in_and_stray_out_are_ignored():
    events = [event(0.5, kind="out"),               # out while not zoomed
              event(1, kind="in"), event(2, kind="in"),   # second in while zoomed
              event(3, kind="out")]
    result = az.normalize_events(events, VP)
    assert [(e.t, e.type) for e in result] == [(1.0, "in"), (3.0, "out")]


# ---------------------------------------------------------------------------
# segments
# ---------------------------------------------------------------------------
def test_pair_in_out_to_one_segment():
    segs = az.build_segments([event(3, kind="in"), event(8, kind="out")], FPS, 0.4, VP)
    assert len(segs) == 1
    seg = segs[0]
    assert (seg.start, seg.up_end, seg.down_start, seg.end) == (75, 85, 200, 210)
    assert (seg.x, seg.y, seg.ramp) == (640, 360, 10)


def test_multiple_non_overlapping_pairs():
    events = [event(2, 100, 100, "in"), event(4, 100, 100, "out"),
              event(10, 900, 500, "in"), event(12, 900, 500, "out")]
    segs = az.build_segments(events, FPS, 0.4, VP)
    assert len(segs) == 2
    assert segs[1].start >= segs[0].end
    assert (segs[1].x, segs[1].y) == (900, 500)


def test_a_new_zoom_waits_until_the_previous_one_is_finished():
    events = [event(3, kind="in"), event(8, kind="out"),
              event(8.2, kind="in"), event(11, kind="out")]   # 8.2 s is inside the previous zoom-out
    segs = az.build_segments(events, FPS, 0.4, VP)
    assert segs[1].start == segs[0].end             # shifted, never overlapping


def test_out_right_after_in_gives_zero_hold():
    segs = az.build_segments([event(3, kind="in"), event(3.1, kind="out")], FPS, 0.4, VP)
    seg = segs[0]
    assert seg.down_start == seg.up_end             # full zoom is reached, then straight back out


def test_open_ended_zoom_holds_to_the_end():
    segs = az.build_segments([event(2, kind="in"), event(4, kind="out"), event(10, 50, 60, "in")],
                             FPS, 0.4, VP)
    last = segs[-1]
    assert last.down_start is None and last.end is None
    assert az.zoom_at_frame(10 ** 6, segs, 2.0) == pytest.approx(2.0)   # still zoomed far past the end


def test_no_events_no_segments():
    assert az.build_segments([], FPS, 0.5, VP) == []
    assert az.build_segments([event(1, kind="out")], FPS, 0.5, VP) == []   # only a stray out


# ---------------------------------------------------------------------------
# zoom curve and crop geometry
# ---------------------------------------------------------------------------
def one_zoom(x=640, y=360):
    return az.build_segments([event(2, x, y, "in"), event(6, x, y, "out")], FPS, 0.4, VP)


def test_zoom_curve_ease_in_hold_ease_out():
    segs = one_zoom()
    z = lambda f: az.zoom_at_frame(f, segs, 2.0)
    assert z(0) == 1.0 and z(49) == 1.0             # before
    assert z(55) == pytest.approx(1.5)              # half of the ramp (start 50, ramp 10)
    assert z(60) == pytest.approx(2.0)              # full zoom reached
    assert z(100) == pytest.approx(2.0)             # hold
    assert z(150) == pytest.approx(2.0)             # out starts at frame 150
    assert z(155) == pytest.approx(1.5)             # half way back
    assert z(160) == 1.0 and z(500) == 1.0          # after
    ramp = [z(f) for f in range(50, 61)]
    assert ramp == sorted(ramp)                     # monotonic ramp


def test_crop_is_centred_on_the_mouse_point():
    segs = one_zoom(640, 360)
    z, x, y, w, h = az.crop_at_frame(100, segs, 2.0, SIZE)
    assert (z, w, h) == (2.0, 640.0, 360.0)
    assert (x, y) == (320.0, 180.0)                 # exactly centred


def test_crop_off_centre_point():
    segs = one_zoom(900, 200)
    _, x, y, w, h = az.crop_at_frame(100, segs, 2.0, SIZE)
    assert (x + w / 2, y + h / 2) == (900.0, 200.0)


def test_crop_is_clamped_at_the_corners():
    _, x, y, w, h = az.crop_at_frame(100, one_zoom(10, 10), 2.0, SIZE)
    assert (x, y) == (0.0, 0.0)
    _, x, y, w, h = az.crop_at_frame(100, one_zoom(1270, 715), 2.0, SIZE)
    assert (x, y) == (640.0, 360.0)                 # right/bottom edge of the frame


def test_crop_always_inside_the_frame_and_contains_the_point():
    for px, py in [(0, 0), (5, 700), (640, 360), (1279, 719), (300, 10)]:
        segs = one_zoom(px, py)
        for frame in range(0, 220):
            z, x, y, w, h = az.crop_at_frame(frame, segs, 2.0, SIZE)
            assert x >= -1e-9 and y >= -1e-9
            assert x + w <= SIZE[0] + 1e-9 and y + h <= SIZE[1] + 1e-9
            if z > 1.0:
                assert x - 1e-9 <= px <= x + w + 1e-9
                assert y - 1e-9 <= py <= y + h + 1e-9


# ---------------------------------------------------------------------------
# generated ffmpeg expressions == the Python model
# ---------------------------------------------------------------------------
def model_segments():
    events = [event(2, 200, 100, "in"), event(5, 200, 100, "out"),
              event(8, 1270, 715, "in"), event(9, 1270, 715, "out"),
              event(12, 640, 360, "in")]                     # open-ended
    return az.build_segments(events, FPS, 0.5, VP)


def test_zoom_expression_matches_the_model():
    segs = model_segments()
    expr = az.zoom_expression(segs, 2.0)
    for frame in range(0, 600, 3):
        assert eval_ffmpeg(expr, on=frame) == pytest.approx(az.zoom_at_frame(frame, segs, 2.0), abs=1e-9)


@pytest.mark.parametrize("supersample", [1, 2])
def test_position_expressions_match_the_model(supersample):
    segs = model_segments()
    zoom_expr = az.zoom_expression(segs, 2.0)
    x_expr, y_expr = az.position_expressions(segs, SIZE, VP, supersample)
    iw, ih = SIZE[0] * supersample, SIZE[1] * supersample
    for frame in range(0, 600, 3):
        z = eval_ffmpeg(zoom_expr, on=frame)
        x = eval_ffmpeg(x_expr, on=frame, iw=iw, zoom=z)
        y = eval_ffmpeg(y_expr, on=frame, ih=ih, zoom=z)
        _, mx, my, _, _ = az.crop_at_frame(frame, segs, 2.0, SIZE)
        assert x == pytest.approx(mx * supersample, abs=1e-6)
        assert y == pytest.approx(my * supersample, abs=1e-6)


def test_mouse_coordinates_are_scaled_when_the_video_size_differs_from_the_viewport():
    segs = az.build_segments([event(1, 640, 360, "in")], FPS, 0.4, VP)       # open-ended
    x_expr, y_expr = az.position_expressions(segs, (2560, 1440), VP, 1)
    x = eval_ffmpeg(x_expr, on=500, iw=2560, zoom=2.0)
    y = eval_ffmpeg(y_expr, on=500, ih=1440, zoom=2.0)
    # the point maps to (1280, 720) in the 2560x1440 frame; a 2x crop is 1280x720 -> top-left (640, 360)
    assert (x, y) == (640.0, 360.0)


def test_filter_chain_structure():
    segs = model_segments()
    vf = az.build_filter(segs, SIZE, VP, FPS, 2.0, supersample=2)
    assert vf.startswith("fps=25,scale=2560:1440:flags=lanczos,zoompan=")
    assert "s=1280x720" in vf and "d=1" in vf
    assert vf.endswith(",format=yuv420p")
    assert " " not in vf
    assert "scale=" not in az.build_filter(segs, SIZE, VP, FPS, 2.0, supersample=1)


def test_command_uses_vp9_and_the_given_ffmpeg_binary():
    cmd = az.build_command("/opt/ff/ffmpeg", "in.webm", "out.webm", "fps=25")
    assert cmd[0] == "/opt/ff/ffmpeg"
    assert cmd[cmd.index("-c:v") + 1] == "libvpx-vp9"
    assert cmd[-1] == "out.webm" and "in.webm" in cmd


# ---------------------------------------------------------------------------
# ffmpeg output parsing
# ---------------------------------------------------------------------------
def test_parse_video_size():
    text = ("  Stream #0:0: Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), "
            "1920x1080 [SAR 1:1 DAR 16:9], 30 fps, 30 tbr")
    assert az.parse_video_size(text) == (1920, 1080)
    assert az.parse_video_size("  Stream #0:0: Video: vp8, yuv420p(tv, progressive), 1280x720, SAR 1:1 DAR 16:9") == (1280, 720)
    assert az.parse_video_size("no video here") is None
    assert az.parse_video_size(None) is None


# ---------------------------------------------------------------------------
# sidecar parsing
# ---------------------------------------------------------------------------
SPEC_SIDECAR = {
    "viewport": {"width": 1280, "height": 720}, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5,
    "events": [{"t": 3.0, "x": 640, "y": 360, "type": "in"}, {"t": 8.0, "x": 640, "y": 360, "type": "out"}],
}


def test_parse_sidecar_from_the_spec():
    sc = az.parse_sidecar(SPEC_SIDECAR)
    assert sc.viewport == (1280, 720) and sc.fps == 25.0
    assert sc.zoom_factor == 2.0 and sc.ramp_sec == 0.5
    assert len(sc.events) == 2


def test_parse_sidecar_defaults_for_missing_fields():
    sc = az.parse_sidecar({"events": [event(1)]})
    assert sc.viewport == az.DEFAULT_VIEWPORT
    assert (sc.fps, sc.zoom_factor, sc.ramp_sec) == (25.0, 2.0, 0.5)


def test_old_sidecar_without_type_is_accepted():
    old = {"viewport": {"width": 1280, "height": 720}, "fps": 25,
           "events": [event(3), event(8)]}                      # no "type", no zoom_factor/ramp_sec
    sc = az.parse_sidecar(old)
    segs = az.build_segments(sc.events, sc.fps, sc.ramp_sec, sc.viewport)
    assert len(segs) == 1 and segs[0].down_start is not None


def test_parse_sidecar_accepts_a_bare_event_list():
    assert len(az.parse_sidecar([event(1), event(2)]).events) == 2


def test_parse_sidecar_rejects_bad_values():
    with pytest.raises(ValueError):
        az.parse_sidecar("not an object")
    with pytest.raises(ValueError):
        az.parse_sidecar({"zoom_factor": 1.0})
    with pytest.raises(ValueError):
        az.parse_sidecar({"zoom_factor": 11})
    with pytest.raises(ValueError):
        az.parse_sidecar({"fps": 0})
    with pytest.raises(ValueError):
        az.parse_sidecar({"ramp_sec": -1})


def test_load_sidecar_reads_a_utf8_file_with_bom(tmp_path):
    path = tmp_path / "a.zoom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(SPEC_SIDECAR).encode("utf-8"))
    assert len(az.load_sidecar(str(path)).events) == 2


def test_find_sidecar_prefers_name_zoom_json_and_falls_back_to_video_zoom_json(tmp_path):
    video = str(tmp_path / "ACME.webm")
    assert az.find_sidecar(video) is None
    fallback = tmp_path / "ACME.webm.zoom.json"
    fallback.write_text("{}")
    assert az.find_sidecar(video) == str(fallback)
    preferred = tmp_path / "ACME.zoom.json"
    preferred.write_text("{}")
    assert az.find_sidecar(video) == str(preferred)


# ---------------------------------------------------------------------------
# processing paths that do not need ffmpeg
# ---------------------------------------------------------------------------
@pytest.fixture
def no_ffmpeg(monkeypatch):
    """Fail loudly if ffmpeg would be started."""
    def boom(*args, **kwargs):
        raise AssertionError("ffmpeg must not be started: {}".format(args))
    monkeypatch.setattr(az.subprocess, "run", boom)


def make_video(tmp_path, name="v.webm"):
    path = tmp_path / name
    path.write_bytes(b"fake video bytes")
    return str(path)


def test_no_sidecar_copies_the_video_unchanged(tmp_path, no_ffmpeg):
    video = make_video(tmp_path)
    result = az.process_video(video, log=lambda m: None)
    assert result.mode == "copy"
    assert result.output == str(tmp_path / "v.zoomed.webm")
    assert open(result.output, "rb").read() == b"fake video bytes"


def test_sidecar_without_events_copies_the_video_unchanged(tmp_path, no_ffmpeg):
    video = make_video(tmp_path)
    (tmp_path / "v.zoom.json").write_text(json.dumps({"viewport": {"width": 1280, "height": 720}, "events": []}))
    result = az.process_video(video, log=lambda m: None)
    assert result.mode == "copy"
    assert open(result.output, "rb").read() == b"fake video bytes"


def test_sidecar_with_only_a_stray_out_copies_unchanged(tmp_path, no_ffmpeg):
    video = make_video(tmp_path)
    (tmp_path / "v.zoom.json").write_text(json.dumps({"events": [event(2, kind="out")]}))
    assert az.process_video(video, log=lambda m: None).mode == "copy"


def test_output_must_differ_from_the_input(tmp_path, no_ffmpeg):
    video = make_video(tmp_path)
    with pytest.raises(ValueError):
        az.process_video(video, output=video, log=lambda m: None)


def test_missing_video_is_an_error(tmp_path, no_ffmpeg):
    with pytest.raises(FileNotFoundError):
        az.process_video(str(tmp_path / "nope.webm"), log=lambda m: None)


def test_dry_run_prints_the_command_and_writes_nothing(tmp_path, monkeypatch, capsys):
    video = make_video(tmp_path)
    (tmp_path / "v.zoom.json").write_text(json.dumps(SPEC_SIDECAR))
    monkeypatch.setattr(az, "probe_size", lambda ffmpeg, v: None)       # no ffmpeg needed
    monkeypatch.setattr(az.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("encoded!")))

    result = az.process_video(video, ffmpeg="/opt/ff/ffmpeg", dry_run=True)

    out = capsys.readouterr().out
    assert "dry run" in out and "zoompan" in out and "libvpx-vp9" in out
    assert result.command[0] == "/opt/ff/ffmpeg"
    assert not (tmp_path / "v.zoomed.webm").exists()
    assert not (tmp_path / "v.zoomed.part.webm").exists()


def test_cli_dry_run(tmp_path, monkeypatch, capsys):
    video = make_video(tmp_path)
    (tmp_path / "v.zoom.json").write_text(json.dumps(SPEC_SIDECAR))
    monkeypatch.setattr(az, "probe_size", lambda ffmpeg, v: None)
    assert az.main(["--video", video, "--dry-run"]) == 0
    assert "ffmpeg -hide_banner" in capsys.readouterr().out


def test_cli_reports_a_missing_video(tmp_path, capsys):
    assert az.main(["--video", str(tmp_path / "missing.webm")]) == 2
    assert "video not found" in capsys.readouterr().err


def test_ffmpeg_binary_that_does_not_exist_gives_a_clear_error(tmp_path, monkeypatch):
    video = make_video(tmp_path)
    (tmp_path / "v.zoom.json").write_text(json.dumps(SPEC_SIDECAR))
    monkeypatch.setattr(az, "probe_size", lambda ffmpeg, v: None)
    with pytest.raises(FileNotFoundError):
        az.process_video(video, ffmpeg="/definitely/not/ffmpeg", log=lambda m: None)


# ---------------------------------------------------------------------------
# integration: real ffmpeg (skipped when ffmpeg is not installed)
# ---------------------------------------------------------------------------
HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")
W, H = 320, 180


def raw_frame(args_before_input, source, vf):
    """One decoded frame as raw gray bytes."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error"] + args_before_input + ["-i", source, "-vf", vf,
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    assert len(proc.stdout) == W * H
    return proc.stdout


def mean_abs_diff(a, b):
    return sum(abs(p - q) for p, q in zip(a, b)) / float(len(a))


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("zoomsrc") / "src.webm")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size={}x{}:rate=25:duration=4".format(W, H),
         "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "10", path], check=True)
    return path


@needs_ffmpeg
def test_real_ffmpeg_zoom_is_centred_on_the_point_and_clamped(tmp_path, sample_video):
    video = str(tmp_path / "v.webm")
    shutil.copy(sample_video, video)
    sidecar = {
        "viewport": {"width": W, "height": H}, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.4,
        "events": [
            {"t": 0.8, "x": 200, "y": 100, "type": "in"}, {"t": 2.0, "x": 200, "y": 100, "type": "out"},
            {"t": 2.6, "x": 5, "y": 5, "type": "in"},                        # open-ended, corner point
        ],
    }
    (tmp_path / "v.zoom.json").write_text(json.dumps(sidecar))

    result = az.process_video(video, ffmpeg="ffmpeg", log=lambda m: None)

    assert result.mode == "zoom" and os.path.isfile(result.output)
    assert az.probe_size("ffmpeg", result.output) == (W, H)                 # resolution preserved

    def check(frame, crop, must_differ_from_plain=True):
        """Output frame == crop of the SOURCE frame, scaled up."""
        x, y, w, h = crop
        out = raw_frame([], result.output, "select=eq(n\\,{})".format(frame))
        expected = raw_frame([], sample_video, "select=eq(n\\,{}),crop={}:{}:{}:{},scale={}:{}".format(
            frame, w, h, x, y, W, H))
        plain = raw_frame([], sample_video, "select=eq(n\\,{})".format(frame))
        close = mean_abs_diff(out, expected)
        assert close < 8, "frame {}: zoomed frame differs from the expected crop ({})".format(frame, close)
        if must_differ_from_plain:
            assert mean_abs_diff(out, plain) > 2 * close

    check(40, (120, 55, 160, 90))       # point (200,100): x=200-80, y=100-45 -> centred
    check(90, (0, 0, 160, 90))          # point (5,5): clamped to the top-left corner, held to the end
    # before the first zoom nothing changed
    out0 = raw_frame([], result.output, "select=eq(n\\,5)")
    src0 = raw_frame([], sample_video, "select=eq(n\\,5)")
    assert mean_abs_diff(out0, src0) < 8


@needs_ffmpeg
def test_real_ffmpeg_ramp_is_smooth(tmp_path, sample_video):
    """The crop width must change a little every frame in the ramp (no jumps) and stay put in the hold."""
    video = str(tmp_path / "v.webm")
    shutil.copy(sample_video, video)
    sidecar = {"viewport": {"width": W, "height": H}, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.4,
               "events": [{"t": 1.0, "x": 160, "y": 90, "type": "in"}]}
    (tmp_path / "v.zoom.json").write_text(json.dumps(sidecar))
    result = az.process_video(video, ffmpeg="ffmpeg", log=lambda m: None)
    assert result.mode == "zoom"
    segs = az.build_segments(sidecar["events"], 25.0, 0.4, (W, H))
    widths = [az.crop_at_frame(f, segs, 2.0, (W, H))[3] for f in range(20, 40)]
    steps = [a - b for a, b in zip(widths, widths[1:])]
    assert all(step >= 0 for step in steps) and max(steps) < 0.25 * W      # no jump
    assert widths[-1] == pytest.approx(W / 2.0)                              # full zoom
