"""Check Fathom's official download API end to end (what the VALR player relies on).

    python scripts/fathom_media_smoke.py [--recording-id ID | --pick N] [--env-file PATH] [--wait 300]

Without an id it takes the newest recording from `GET /meetings`. It requests a download
(`POST /recordings/{id}/download`), polls `GET /recordings/{id}/downloads/{download_id}`
until it completes, then probes the signed video URL with a HEAD and a 1 KB Range GET.

It prints only: the storage host, content type, size, whether byte ranges work, and how
long the signed URL lives. Never the key, the signed URL, titles or any transcript.
FATHOM_API_KEY is read from the environment, else from --env-file (default: this
checkout's .env). On the VM: `sudo docker exec voice-agent python scripts/fathom_media_smoke.py`.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.fathom.ai/external/v1"


def key_from(env_file: Path) -> str:
    key = os.getenv("FATHOM_API_KEY", "").strip()
    if key or not env_file.exists():
        return key
    from dotenv import dotenv_values
    return str(dotenv_values(env_file).get("FATHOM_API_KEY") or "").strip()


def fail(what: str, response: httpx.Response) -> None:
    raise SystemExit(f"Fathom {what}: HTTP {response.status_code}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording-id", default="")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--pick", type=int, default=0, help="0 = newest, 1 = the one before, ...")
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for the video")
    args = parser.parse_args()
    key = key_from(args.env_file)
    if not key:
        raise SystemExit("No FATHOM_API_KEY in the environment or the env file.")
    with httpx.Client(headers={"X-Api-Key": key, "Accept": "application/json"}, timeout=30) as api:
        recording_id = args.recording_id
        if not recording_id:
            listed = api.get(f"{API}/meetings")
            if listed.status_code != 200:
                fail("list meetings", listed)
            items = [i for i in listed.json().get("items") or [] if i.get("recording_id")]
            if not items:
                raise SystemExit("No recordings on this account.")
            recording_id = str(items[min(args.pick, len(items) - 1)]["recording_id"])
        print(f"recording: #{args.pick} newest on the account" if not args.recording_id else "recording: as given")
        started = time.monotonic()
        made = api.post(f"{API}/recordings/{recording_id}/download", json={})
        print(f"request download: HTTP {made.status_code}")
        if made.status_code not in (200, 201, 202):
            fail("request download", made)
        data = made.json()
        download_id = data.get("download_id")
        while data.get("status") == "processing" and time.monotonic() - started < args.wait:
            time.sleep(5)
            polled = api.get(f"{API}/recordings/{recording_id}/downloads/{download_id}")
            if polled.status_code != 200:
                fail("poll download", polled)
            data = polled.json()
        elapsed = time.monotonic() - started
        print(f"status: {data.get('status')} after {elapsed:.0f} s"
              + (f" ({data.get('failure_reason')})" if data.get("failure_reason") else ""))
    for kind in ("video", "audio"):
        media = data.get(kind) or {}
        if not media.get("url"):
            continue
        url = media["url"]
        expires = media.get("expires_at")
        life = ""
        if expires:
            moment = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            life = f"{(moment - datetime.now(timezone.utc)).total_seconds() / 3600:.1f} h from now"
        print(f"{kind}: host={urlsplit(url).hostname} content_type={media.get('content_type')} "
              f"size={media.get('file_size_bytes')} expires_in={life or 'not given'}")
        with httpx.Client(timeout=30, follow_redirects=False) as storage:
            head = storage.head(url)
            print(f"  HEAD: HTTP {head.status_code} accept-ranges={head.headers.get('accept-ranges')} "
                  f"content-type={head.headers.get('content-type')} "
                  f"cors={head.headers.get('access-control-allow-origin')}")
            if head.is_redirect:
                print(f"  redirects to host={urlsplit(head.headers.get('location', '')).hostname}")
            part = storage.get(url, headers={"Range": "bytes=0-1023"})
            print(f"  Range GET 0-1023: HTTP {part.status_code} bytes={len(part.content)} "
                  f"content-range={'yes' if part.headers.get('content-range') else 'no'}")
    if data.get("status") != "completed":
        sys.exit(1)


if __name__ == "__main__":
    main()
