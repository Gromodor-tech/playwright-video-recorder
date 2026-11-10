"""
video_client.py - client functions used by the recorder (capture_video.py).

`requests` is imported lazily inside the functions, so the recorder keeps
working without it when --server is not used.
"""

import mimetypes
import os


def UploadVideo(server_url: str, video_path: str, company_name: str) -> str:
    """Uploads a video file to the server and returns its URL."""
    import requests  # lazy import

    url = server_url.rstrip("/") + "/upload"
    filename = os.path.basename(video_path)
    mime = mimetypes.guess_type(filename)[0] or "video/webm"

    with open(video_path, "rb") as fileobj:
        response = requests.post(
            url,
            files={"file": (filename, fileobj, mime)},
            data={"company_name": company_name},
            timeout=300,
        )
    response.raise_for_status()
    return response.json()["video_url"]


def GetVideoURL(server_url: str, company_name: str) -> str:
    """Returns the video URL for the given company name."""
    import requests  # lazy import

    url = server_url.rstrip("/") + "/get_video_url"
    response = requests.get(url, params={"company_name": company_name}, timeout=30)
    response.raise_for_status()                      # 404 for an unknown company
    return response.json()["video_url"]
