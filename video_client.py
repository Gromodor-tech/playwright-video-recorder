"""
video_client.py - client functions used by the recorder (capture_video.py).

`requests` is imported lazily inside the functions, so the recorder keeps
working without it when --server is not used.
"""

import mimetypes
import os
import time


def _sidecar_path(video_path):
    """ACME_GmbH.webm -> ACME_GmbH.zoom.json (written by qscript.py only when zoom events exist)."""
    return os.path.splitext(video_path)[0] + ".zoom.json"


def UploadVideo(server_url: str, video_path: str, company_name: str) -> str:
    """Uploads a video file to the server and returns its URL.

    If the recorder wrote a zoom sidecar (<video>.zoom.json) next to the video, it is
    uploaded too and the server queues the video for the zoom post-processing.
    """
    import requests  # lazy import

    url = server_url.rstrip("/") + "/upload"
    filename = os.path.basename(video_path)
    mime = mimetypes.guess_type(filename)[0] or "video/webm"
    sidecar = _sidecar_path(video_path)

    video_file = open(video_path, "rb")
    sidecar_file = open(sidecar, "rb") if os.path.isfile(sidecar) else None
    try:
        files = {"file": (filename, video_file, mime)}
        if sidecar_file is not None:
            files["zoom"] = (os.path.basename(sidecar), sidecar_file, "application/json")
        response = requests.post(url, files=files, data={"company_name": company_name}, timeout=300)
    finally:
        video_file.close()
        if sidecar_file is not None:
            sidecar_file.close()
    response.raise_for_status()
    return response.json()["video_url"]


def GetVideoURL(server_url: str, company_name: str) -> str:
    """Returns the video URL for the given company name."""
    import requests  # lazy import

    url = server_url.rstrip("/") + "/get_video_url"
    response = requests.get(url, params={"company_name": company_name}, timeout=30)
    response.raise_for_status()                      # 404 for an unknown company
    return response.json()["video_url"]


# ---------------------------------------------------------------------------
# Zoom processing (Internship Task 2)
# ---------------------------------------------------------------------------
def GetJob(server_url: str, company_name: str) -> dict:
    """Processing job of a company: {"id", "process_status", "processed_video_url", ...}."""
    import requests  # lazy import

    url = server_url.rstrip("/") + "/jobs"
    response = requests.get(url, params={"company_name": company_name}, timeout=30)
    response.raise_for_status()
    jobs = response.json()
    if not jobs:
        raise LookupError("no such company: " + company_name)
    return jobs[0]


def ProcessVideo(server_url: str, company_name: str) -> dict:
    """Queue the zoom processing of an uploaded video (POST /jobs/<id>/process)."""
    import requests  # lazy import

    job = GetJob(server_url, company_name)
    url = "{}/jobs/{}/process".format(server_url.rstrip("/"), job["id"])
    response = requests.post(url, timeout=30)
    response.raise_for_status()
    return response.json()


def WaitForProcessed(server_url: str, company_name: str, timeout_sec: float = 300,
                     poll_sec: float = 2.0) -> dict:
    """Poll until the zoom job is 'done' or 'failed' (or the timeout passes). Returns the job."""
    deadline = time.time() + timeout_sec
    while True:
        job = GetJob(server_url, company_name)
        if job["process_status"] in ("done", "failed") or time.time() >= deadline:
            return job
        time.sleep(poll_sec)
