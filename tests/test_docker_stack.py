"""Integration tests for the Docker stack (Internship Task 1 + 2).

Skipped unless RUN_DOCKER_TESTS=1. Start the stack first:

    docker compose up -d --build
    RUN_DOCKER_TESTS=1 python -m pytest tests/test_docker_stack.py -v

Needs `requests`; a sample video comes from SAMPLE_VIDEO or is generated with a local ffmpeg.
"""
import json
import os
import shutil
import subprocess
import time

import pytest

requests = pytest.importorskip("requests")

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1",
                       reason="set RUN_DOCKER_TESTS=1 and start the stack: docker compose up -d --build"),
]

BASE = os.environ.get("SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
DB_USER = os.environ.get("DB_USER", "video_user")
DB_NAME = os.environ.get("DB_NAME", "videos")


def compose_exec(service, *command):
    return subprocess.run(["docker", "compose", "exec", "-T", service] + list(command),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


@pytest.mark.parametrize("service", ["web", "worker"])
def test_ffmpeg_is_installed_in_the_image(service):
    version = compose_exec(service, "ffmpeg", "-version")
    assert version.returncode == 0 and "ffmpeg version" in version.stdout
    encoders = compose_exec(service, "ffmpeg", "-hide_banner", "-encoders")
    assert "libvpx-vp9" in encoders.stdout          # the output codec of apply_zoom.py


def test_the_stack_is_healthy():
    response = requests.get(BASE + "/health", timeout=10)
    assert response.status_code == 200 and response.json() == {"status": "ok"}


@pytest.fixture
def sample_video(tmp_path):
    given = os.environ.get("SAMPLE_VIDEO")
    if given and os.path.isfile(given):
        return given
    if shutil.which("ffmpeg") is None:
        pytest.skip("no SAMPLE_VIDEO and no local ffmpeg to generate one")
    path = str(tmp_path / "sample.webm")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=6",
                    "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "35", path], check=True)
    return path


def test_uploaded_video_is_zoomed_and_the_result_is_stored_in_postgresql(sample_video):
    company = "Docker Zoom Test {}".format(int(time.time()))
    sidecar = {"viewport": {"width": 1280, "height": 720}, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5,
               "events": [{"t": 1.0, "x": 640, "y": 360, "type": "in"}, {"t": 3.5, "x": 640, "y": 360, "type": "out"}]}

    with open(sample_video, "rb") as video:
        response = requests.post(BASE + "/upload", timeout=60,
                                 data={"company_name": company},
                                 files={"file": ("sample.webm", video, "video/webm"),
                                        "zoom": ("sample.zoom.json", json.dumps(sidecar), "application/json")})
    assert response.status_code == 200
    original_url = response.json()["video_url"]

    job = requests.get(BASE + "/jobs", params={"company_name": company}, timeout=10).json()[0]
    assert job["process_status"] in ("pending", "processing", "done")      # queued automatically

    deadline = time.time() + 240
    while job["process_status"] not in ("done", "failed") and time.time() < deadline:
        time.sleep(2)
        job = requests.get("{}/jobs/{}".format(BASE, job["id"]), timeout=10).json()
    assert job["process_status"] == "done", job.get("process_error")

    processed_url = job["processed_video_url"]
    assert processed_url and processed_url != original_url
    media = requests.get(processed_url, timeout=30)
    assert media.status_code == 200 and media.headers["Content-Type"].startswith("video/") and len(media.content) > 1000

    api = requests.get(BASE + "/get_processed_video_url", params={"company_name": company}, timeout=10)
    assert api.status_code == 200 and api.json()["processed_video_url"] == processed_url

    # the result really lives in PostgreSQL
    sql = "SELECT processed_video_url FROM videos WHERE company_name = '{}'".format(company)
    stored = compose_exec("db", "psql", "-U", DB_USER, "-d", DB_NAME, "-tA", "-c", sql)
    assert stored.stdout.strip() == processed_url
