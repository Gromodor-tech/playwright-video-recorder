"""
apply_zoom.py - Camtasia-style zoom post-processing (ffmpeg zoompan).

Reads the sidecar <video>.zoom.json written by qscript.py while recording and renders a new
.webm where every in -> out pair is a smooth zoom-in -> hold -> zoom-out around the mouse
position of the "in" event.

    python apply_zoom.py --video video/ACME_GmbH.webm
    python apply_zoom.py --video video/ACME_GmbH.webm --dry-run
    python apply_zoom.py --video video/ACME_GmbH.webm --ffmpeg C:\\tools\\ffmpeg\\bin\\ffmpeg.exe

Only the Python standard library is used (ffmpeg is an external program).

Design notes
------------
* Everything is computed in FRAMES (ffmpeg's zoompan variable `on` is the output frame number),
  so the generated expressions contain only integers and a few constants.
* zoom(frame)   = 1 + (zoom_factor - 1) * sum(pulse_i(frame))     (pairs never overlap)
  pulse_i       = ease(0 -> 1) over `ramp` frames starting at the "in" frame,
                  1 while holding (the time between the two F5 presses),
                  1 - ease(0 -> 1) over `ramp` frames starting at the "out" frame, then 0.
  ease(u)       = u*u*(3-2u)  (smoothstep: ease-in AND ease-out, no robotic linear ramps)
* crop          = (iw/zoom) x (ih/zoom), x = clamp(px - iw/(2*zoom), 0, iw - iw/zoom), same for y
* Mouse pixels map 1:1 to video pixels (record_video_size == viewport). If the real video size
  differs from the sidecar viewport the coordinates are scaled by video_size / viewport.
* The input is up-scaled (--supersample, default 2) before zoompan: zoompan crops at integer
  pixel positions, so working at 2x halves the sub-pixel "shaking" of a slow zoom.
* An "in" without a following "out" (recording ended while zoomed) holds to the end of the video.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

DEFAULT_FPS = 25.0
DEFAULT_ZOOM_FACTOR = 2.0
DEFAULT_RAMP_SEC = 0.5
DEFAULT_VIEWPORT = (1280, 720)
MAX_ZOOM_FACTOR = 10.0          # zoompan itself limits the zoom to 1..10
DEFAULT_SUPERSAMPLE = 2


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Event:
    t: float                    # seconds since video start
    x: float                    # px (viewport / video coordinates)
    y: float
    type: str                   # "in" | "out"


@dataclass(frozen=True)
class Segment:
    """One zoom: all values are FRAME numbers of the output video."""
    start: int                  # first frame of the zoom-in ramp
    up_end: int                 # start + ramp: full zoom reached
    down_start: Optional[int]   # first frame of the zoom-out ramp; None = hold to the end
    end: Optional[int]          # down_start + ramp: back to 1.0; None = open-ended
    x: float
    y: float
    ramp: int                   # ramp length in frames


@dataclass
class Sidecar:
    viewport: Tuple[int, int]
    fps: float
    zoom_factor: float
    ramp_sec: float
    events: List[dict]


# ---------------------------------------------------------------------------
# Pure math (no ffmpeg needed) - unit tested
# ---------------------------------------------------------------------------
def clamp(value, low, high):
    return max(low, min(high, value))


def ease_in_out(u):
    """Smoothstep: 0 -> 1 with zero slope at both ends (ease-in + ease-out)."""
    u = clamp(u, 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


def time_to_frame(t, fps):
    """Seconds -> nearest frame number (never negative)."""
    return max(0, int(math.floor(t * fps + 0.5)))


def ramp_frames(ramp_sec, fps):
    """Length of one ramp in frames (at least 1, so the expressions never divide by zero)."""
    return max(1, int(math.floor(ramp_sec * fps + 0.5)))


def normalize_events(events, viewport=DEFAULT_VIEWPORT):
    """Clean raw sidecar events and return a valid in/out sequence.

    * invalid entries (no numeric t) are skipped, coordinates are clamped to the viewport,
      missing coordinates default to the frame centre;
    * events are sorted by time (stable);
    * an event WITHOUT "type" (old sidecars) is a toggle: it is "in" when not zoomed, else "out";
    * an "in" while already zoomed and an "out" while not zoomed are ignored.
    """
    vw, vh = viewport
    cleaned = []
    for raw in events or []:
        if not isinstance(raw, dict):
            continue
        try:
            t = float(raw["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isnan(t) or math.isinf(t):
            continue
        try:
            x = float(raw.get("x", vw / 2.0))
            y = float(raw.get("y", vh / 2.0))
        except (TypeError, ValueError):
            x, y = vw / 2.0, vh / 2.0
        kind = raw.get("type")
        kind = kind.lower() if isinstance(kind, str) else None
        if kind not in ("in", "out"):
            kind = None
        cleaned.append((max(0.0, t), clamp(x, 0.0, vw), clamp(y, 0.0, vh), kind))
    cleaned.sort(key=lambda item: item[0])

    result = []
    zoomed = False
    for t, x, y, kind in cleaned:
        if kind is None:
            kind = "out" if zoomed else "in"
        if kind == "in" and not zoomed:
            zoomed = True
            result.append(Event(t, x, y, "in"))
        elif kind == "out" and zoomed:
            zoomed = False
            result.append(Event(t, x, y, "out"))
        # otherwise: duplicate in / stray out -> ignored
    return result


def build_segments(events, fps, ramp_sec, viewport=DEFAULT_VIEWPORT):
    """Pair every "in" with the next "out" -> list of non-overlapping Segments (frames).

    * the hold is the time between the two presses: ramp-in starts at the "in" press, the
      ramp-out starts at the "out" press; if the presses are closer than one ramp the hold is 0;
    * a zoom never starts before the previous one is completely finished (no overlap);
    * an "in" without an "out" -> open-ended segment: hold to the end of the video.
    """
    ramp = ramp_frames(ramp_sec, fps)
    ordered = normalize_events(events, viewport)
    segments = []
    previous_end = 0
    pending = None
    for ev in ordered:
        if ev.type == "in":
            pending = ev
            continue
        # "out": close the pending zoom
        start = max(time_to_frame(pending.t, fps), previous_end)
        up_end = start + ramp
        down_start = max(time_to_frame(ev.t, fps), up_end)
        end = down_start + ramp
        segments.append(Segment(start, up_end, down_start, end, pending.x, pending.y, ramp))
        previous_end = end
        pending = None
    if pending is not None:
        start = max(time_to_frame(pending.t, fps), previous_end)
        segments.append(Segment(start, start + ramp, None, None, pending.x, pending.y, ramp))
    return segments


def pulse_at_frame(frame, seg):
    """0..1 amount of zoom of ONE segment at an output frame number."""
    if frame < seg.start:
        return 0.0
    if frame < seg.up_end:
        return ease_in_out((frame - seg.start) / float(seg.ramp))
    if seg.down_start is None or frame < seg.down_start:
        return 1.0
    if frame < seg.end:
        return 1.0 - ease_in_out((frame - seg.down_start) / float(seg.ramp))
    return 0.0


def zoom_at_frame(frame, segments, zoom_factor):
    """Zoom factor (1.0 = no zoom) at an output frame number."""
    return 1.0 + (zoom_factor - 1.0) * sum(pulse_at_frame(frame, s) for s in segments)


def active_segment(frame, segments):
    """The segment whose centre applies at this frame (the first one not finished yet)."""
    for seg in segments:
        if seg.end is None or frame < seg.end:
            return seg
    return segments[-1] if segments else None


def crop_at_frame(frame, segments, zoom_factor, size):
    """Crop rectangle (zoom, x, y, w, h) in video pixels - what the viewer sees at this frame.

    The crop is centred on the mouse point and clamped to the frame:
        x = clamp(px - W/(2*zoom), 0, W - W/zoom)     (same for y)
    """
    width, height = size
    z = zoom_at_frame(frame, segments, zoom_factor)
    seg = active_segment(frame, segments)
    crop_w, crop_h = width / z, height / z
    px = seg.x if seg else width / 2.0
    py = seg.y if seg else height / 2.0
    x = clamp(px - crop_w / 2.0, 0.0, width - crop_w)
    y = clamp(py - crop_h / 2.0, 0.0, height - crop_h)
    return z, x, y, crop_w, crop_h


# ---------------------------------------------------------------------------
# ffmpeg filter generation
# ---------------------------------------------------------------------------
def _num(value):
    """Plain decimal number for an ffmpeg expression (no exponent, no spaces)."""
    text = "%.6f" % float(value)
    text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _smooth(u_expr):
    """ffmpeg text of ease_in_out(u): (u)*(u)*(3-2*(u))."""
    return "(({u})*({u})*(3-2*({u})))".format(u=u_expr)


def _pulse_expr(seg):
    """ffmpeg expression (variable `on` = output frame) for pulse_at_frame()."""
    ramp = _num(seg.ramp)
    up = _smooth("(on-{})/{}".format(_num(seg.start), ramp))
    if seg.down_start is None:
        return "if(lt(on,{a}),0,if(lt(on,{b}),{up},1))".format(
            a=_num(seg.start), b=_num(seg.up_end), up=up)
    down = _smooth("(on-{})/{}".format(_num(seg.down_start), ramp))
    return ("if(lt(on,{a}),0,if(lt(on,{b}),{up},if(lt(on,{c}),1,"
            "if(lt(on,{d}),1-{down},0))))").format(
        a=_num(seg.start), b=_num(seg.up_end), c=_num(seg.down_start),
        d=_num(seg.end), up=up, down=down)


def zoom_expression(segments, zoom_factor):
    """zoompan `z` expression: 1 + (zoom_factor-1) * (pulse_1 + pulse_2 + ...)."""
    pulses = "+".join(_pulse_expr(s) for s in segments)
    return "1+{}*({})".format(_num(zoom_factor - 1.0), pulses)


def _center_expression(segments, values):
    """Pick the centre coordinate of the segment that is active at frame `on`."""
    expr = _num(values[-1])
    for seg, value in reversed(list(zip(segments, values))[:-1]):
        expr = "if(lt(on,{end}),{v},{rest})".format(end=_num(seg.end), v=_num(value), rest=expr)
    return expr


def position_expressions(segments, size, viewport, supersample=DEFAULT_SUPERSAMPLE):
    """zoompan `x` / `y` expressions: crop centred on the mouse point, clamped to the frame.

        x = clip(px - iw/(2*zoom), 0, iw - iw/zoom)        (same for y with ih)

    px/py are the mouse coordinates mapped to the (supersampled) video pixels.
    """
    width, height = size
    ss = max(1, int(supersample))
    kx = width / float(viewport[0]) * ss       # mouse px -> supersampled video px
    ky = height / float(viewport[1]) * ss
    px = _center_expression(segments, [s.x * kx for s in segments])
    py = _center_expression(segments, [s.y * ky for s in segments])
    x = "clip({px}-iw/(2*zoom),0,iw-iw/zoom)".format(px=px)
    y = "clip({py}-ih/(2*zoom),0,ih-ih/zoom)".format(py=py)
    return x, y


def build_filter(segments, size, viewport, fps, zoom_factor, supersample=DEFAULT_SUPERSAMPLE):
    """Complete -vf filter chain: constant fps -> supersample -> zoompan -> yuv420p.

    size     - real video size (output keeps it)
    viewport - size the mouse coordinates refer to (usually identical -> 1:1 mapping)
    """
    width, height = size
    ss = max(1, int(supersample))
    z = zoom_expression(segments, zoom_factor)
    x, y = position_expressions(segments, size, viewport, ss)
    parts = ["fps={}".format(_num(fps))]
    if ss > 1:
        parts.append("scale={}:{}:flags=lanczos".format(width * ss, height * ss))
    parts.append("zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps}".format(
        z=z, x=x, y=y, w=width, h=height, fps=_num(fps)))
    parts.append("format=yuv420p")
    return ",".join(parts)


def build_command(ffmpeg, video, output, vf, crf=30):
    """ffmpeg command line (list) producing the zoomed VP9 .webm."""
    return [
        ffmpeg, "-hide_banner", "-y", "-i", video,
        "-vf", vf,
        "-an",
        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(crf),
        "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
        output,
    ]


_SIZE_RE = re.compile(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})(?=[\s,\[])")


def parse_video_size(ffmpeg_stderr):
    """Extract (width, height) from the text `ffmpeg -i file` prints; None if not found."""
    match = _SIZE_RE.search(ffmpeg_stderr or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def probe_size(ffmpeg, video):
    """Real video size via `ffmpeg -i` (no ffprobe needed); None when it cannot be read."""
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", video],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, errors="replace")
    except OSError:
        return None
    return parse_video_size(proc.stderr)


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------
def sidecar_candidates(video):
    stem = os.path.splitext(video)[0]
    return [stem + ".zoom.json", video + ".zoom.json"]


def find_sidecar(video):
    for path in sidecar_candidates(video):
        if os.path.isfile(path):
            return path
    return None


def parse_sidecar(data):
    """dict (or a bare list of events) -> Sidecar with defaults for missing fields."""
    if isinstance(data, list):
        data = {"events": data}
    if not isinstance(data, dict):
        raise ValueError("zoom sidecar must be a JSON object")

    viewport = DEFAULT_VIEWPORT
    vp = data.get("viewport")
    if isinstance(vp, dict):
        try:
            w, h = int(vp["width"]), int(vp["height"])
            if w > 0 and h > 0:
                viewport = (w, h)
        except (KeyError, TypeError, ValueError):
            pass

    def number(key, default):
        try:
            return float(data.get(key, default))
        except (TypeError, ValueError):
            return default

    fps = number("fps", DEFAULT_FPS)
    zoom_factor = number("zoom_factor", DEFAULT_ZOOM_FACTOR)
    ramp_sec = number("ramp_sec", DEFAULT_RAMP_SEC)
    if fps <= 0:
        raise ValueError("fps must be > 0")
    if not (1.0 < zoom_factor <= MAX_ZOOM_FACTOR):
        raise ValueError("zoom_factor must be in (1, {}]".format(int(MAX_ZOOM_FACTOR)))
    if ramp_sec < 0:
        raise ValueError("ramp_sec must be >= 0")
    events = data.get("events")
    if not isinstance(events, list):
        events = []
    return Sidecar(viewport, fps, zoom_factor, ramp_sec, events)


def load_sidecar(path):
    with open(path, "r", encoding="utf-8-sig") as handle:
        return parse_sidecar(json.load(handle))


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
@dataclass
class ProcessResult:
    mode: str                   # "zoom" | "copy" (no zoom events -> copied unchanged)
    output: str
    command: Optional[List[str]]
    segments: int


def default_output_path(video):
    return os.path.splitext(video)[0] + ".zoomed.webm"


def process_video(video, output=None, sidecar=None, ffmpeg="ffmpeg", dry_run=False,
                  zoom_factor=None, ramp_sec=None, supersample=DEFAULT_SUPERSAMPLE,
                  crf=30, log=print):
    """Render the zoomed video. Without a sidecar / without events the video is copied unchanged."""
    if not os.path.isfile(video):
        raise FileNotFoundError("video not found: " + video)
    output = output or default_output_path(video)
    if os.path.abspath(output) == os.path.abspath(video):
        raise ValueError("output must differ from the input video")

    sidecar_path = sidecar or find_sidecar(video)
    data = load_sidecar(sidecar_path) if sidecar_path else None
    segments = []
    if data is not None:
        factor = zoom_factor if zoom_factor is not None else data.zoom_factor
        ramp = ramp_sec if ramp_sec is not None else data.ramp_sec
        if not (1.0 < factor <= MAX_ZOOM_FACTOR):
            raise ValueError("zoom_factor must be in (1, {}]".format(int(MAX_ZOOM_FACTOR)))
        segments = build_segments(data.events, data.fps, ramp, data.viewport)

    if not segments:
        log("no zoom events -> the video is copied unchanged" +
            (" (dry run: nothing written)" if dry_run else ""))
        if not dry_run:
            _copy_atomic(video, output)
        return ProcessResult("copy", output, None, 0)

    size = probe_size(ffmpeg, video) or data.viewport
    vf = build_filter(segments, size, data.viewport, data.fps, factor, supersample)
    part = os.path.splitext(output)[0] + ".part.webm"
    command = build_command(ffmpeg, video, part, vf, crf)

    log("{} zoom segment(s), video size {}x{}, {} fps".format(len(segments), size[0], size[1], _num(data.fps)))
    if dry_run:
        log("ffmpeg command (dry run, nothing is encoded):")
        log(_format_command(command[:-1] + [output]))
        return ProcessResult("zoom", output, command[:-1] + [output], len(segments))

    if shutil.which(ffmpeg) is None and not os.path.isfile(ffmpeg):
        raise FileNotFoundError("ffmpeg not found: {} (install it or pass --ffmpeg <path>)".format(ffmpeg))
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, errors="replace")
    if proc.returncode != 0:
        _remove_quietly(part)
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        raise RuntimeError("ffmpeg failed (exit {}):\n{}".format(proc.returncode, tail))
    os.replace(part, output)
    log("written: " + output)
    return ProcessResult("zoom", output, command[:-1] + [output], len(segments))


def _quote(arg):
    """Quote one argument for display: plain words stay as they are, everything else goes
    into double quotes (the same text can be pasted into bash, PowerShell and cmd)."""
    if arg and not re.search(r"[\s'\"(),;&|<>$`]", arg):
        return arg
    return '"' + arg.replace('"', '\\"') + '"'


def _format_command(command):
    return " ".join(_quote(part) for part in command)


def _copy_atomic(source, destination):
    part = destination + ".part"
    shutil.copyfile(source, part)
    os.replace(part, destination)


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        description="Apply the Camtasia-style zoom recorded in <video>.zoom.json to a video.")
    parser.add_argument("--video", required=True, help="input .webm (its sidecar <name>.zoom.json is used)")
    parser.add_argument("--output", "-o", help="output file (default: <name>.zoomed.webm)")
    parser.add_argument("--sidecar", help="sidecar file (default: <name>.zoom.json next to the video)")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="path to the ffmpeg binary (default: ffmpeg from PATH)")
    parser.add_argument("--dry-run", action="store_true", help="print the ffmpeg command, do not encode")
    parser.add_argument("--zoom-factor", type=float, help="override zoom_factor from the sidecar")
    parser.add_argument("--ramp-sec", type=float, help="override ramp_sec from the sidecar")
    parser.add_argument("--supersample", type=int, default=DEFAULT_SUPERSAMPLE,
                        help="internal up-scale factor against zoompan jitter (default 2, 1 = off)")
    parser.add_argument("--crf", type=int, default=30, help="VP9 quality, lower = better (default 30)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = process_video(
            args.video, output=args.output, sidecar=args.sidecar, ffmpeg=args.ffmpeg,
            dry_run=args.dry_run, zoom_factor=args.zoom_factor, ramp_sec=args.ramp_sec,
            supersample=args.supersample, crf=args.crf)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2
    if not args.dry_run:
        print("{}: {}".format(result.mode, result.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
