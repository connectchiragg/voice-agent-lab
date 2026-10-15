"""Download a phone call recording (TELNYX_RECORD=1) from Telnyx.

    python scripts/fetch_recording.py [RECORDING_ID] [--out call.wav] [--env-file PATH]

Without an id, the latest recording made through this app's Call Control
application (`voice-agent-lab`) in the last --days days. The recording ids the
server stored are in Postgres on the VM:

    sudo docker exec voice-postgres psql -U voice_agent -d voice_agent \\
      -c 'SELECT * FROM call_recordings ORDER BY created_at DESC LIMIT 5'

TELNYX_API_KEY is read from --env-file (default: this checkout's .env). Download
URLs from Telnyx are signed and expire after 10 minutes, so a fresh one is
fetched each time. Neither the key nor the URL is ever printed. Recordings are
private call audio: never commit them.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
API_URL = "https://api.telnyx.com/v2"
APPLICATION_NAME = "voice-agent-lab"


def get(client: httpx.Client, path: str, params: dict[str, str] | None = None) -> dict:
    response = client.get(f"{API_URL}/{path}", params=params)
    if not response.is_success:
        raise SystemExit(f"Telnyx GET {path.split('/')[0]}: HTTP {response.status_code}")
    return response.json()


def latest_id(client: httpx.Client, days: int) -> str:
    """Newest recording on this app's Call Control connection (not other Telnyx apps)."""
    apps = get(client, "call_control_applications",
               {"filter[application_name][contains]": APPLICATION_NAME})["data"]
    connections = {str(app["id"]) for app in apps if app.get("application_name") == APPLICATION_NAME}
    if not connections:
        raise SystemExit(f"No Call Control application named {APPLICATION_NAME}.")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = []
    for connection in connections:
        found += get(client, "recordings", {"filter[connection_id]": connection,
                                            "filter[created_at][gte]": since,
                                            "page[size]": "250"})["data"]
    if not found:
        raise SystemExit(f"No recordings in the last {days} days.")
    return str(max(found, key=lambda item: str(item.get("created_at", "")))["id"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recording_id", nargs="?")
    parser.add_argument("--out", type=Path, help="Default: recording-<id>.wav here.")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--format", choices=("wav", "mp3"), default="wav")
    args = parser.parse_args()
    key = dotenv_values(args.env_file).get("TELNYX_API_KEY")
    if not key:
        raise SystemExit(f"TELNYX_API_KEY is not set in {args.env_file}.")
    with httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=30) as client:
        recording_id = args.recording_id or latest_id(client, args.days)
        recording = get(client, f"recordings/{recording_id}")["data"]
        url = (recording.get("download_urls") or {}).get(args.format)
        if not url:
            raise SystemExit(f"Recording {recording_id} has no {args.format} download.")
    out = args.out or Path(f"recording-{recording_id}.{args.format}")
    # The signed URL goes to Telnyx's storage, not the API: no Authorization header.
    with httpx.stream("GET", url, timeout=60, follow_redirects=True) as response:
        if not response.is_success:
            raise SystemExit(f"Download: HTTP {response.status_code}")
        with out.open("wb") as file:
            for chunk in response.iter_bytes():
                file.write(chunk)
    seconds = (recording.get("duration_millis") or 0) / 1000
    print(f"Recording {recording_id}: {seconds:.1f} s, {recording.get('channels')} channel, "
          f"created {recording.get('created_at')} -> {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    sys.exit(main())
