#!/bin/sh
# Move Telnyx (only the voice-agent-lab Call Control app) from voice.haciensus.com to
# valr.haciensus.com. Run ON THE VM from ~/voice-agent-lab, only after Cloudflare Access
# bypasses exist for valr.haciensus.com/telnyx/webhook and /ws/telnyx.
#   sh deploy/switch-telnyx-host.sh            # switch
#   HOST=voice.haciensus.com sh deploy/switch-telnyx-host.sh   # roll back
set -eu
HOST=${HOST:-valr.haciensus.com}
APP_ID=3071391970178893742   # voice-agent-lab Call Control app. Never any other app or number.
cd "$(dirname "$0")/.."
L=$(sudo -n docker logs --since 15m voice-agent-lab 2>&1 | grep -E "Phone call (connected|ended)" | tail -1 || true)
case "$L" in *connected*) echo "A call may be in progress; try again later." >&2; exit 1;; esac
sudo -n docker exec -i -e HOST="$HOST" -e APP_ID="$APP_ID" voice-callbacks python - <<'PY'
import os, httpx
url = f"https://api.telnyx.com/v2/call_control_applications/{os.environ['APP_ID']}"
headers = {"Authorization": "Bearer " + os.environ["TELNYX_API_KEY"]}
hook = f"https://{os.environ['HOST']}/telnyx/webhook"
r = httpx.patch(url, json={"webhook_event_url": hook}, headers=headers, timeout=20)
r.raise_for_status()
d = httpx.get(url, headers=headers, timeout=20).json()["data"]
print("Telnyx app", d.get("application_name"), "webhook now", d.get("webhook_event_url"))
PY
t=$(mktemp)
grep -v '^TELNYX_STREAM_URL=' .env > "$t"; echo "TELNYX_STREAM_URL=wss://$HOST/ws/telnyx" >> "$t"
cat "$t" > .env; rm -f "$t"; chmod 600 .env
sudo -n docker rm -f voice-agent-lab >/dev/null
sudo -n docker run -d --name voice-agent-lab --restart unless-stopped --env-file .env \
  --network voice-agent-lab $(sh deploy/valr-data-live.sh app) -p 127.0.0.1:7860:7860 voice-agent-lab:latest >/dev/null
sleep 15
curl -s localhost:7860/healthz; echo
echo "Unsigned webhook on https://$HOST/telnyx/webhook (expect 401):"
curl -s -o /dev/null -w "%{http_code}\n" -X POST -d '{}' "https://$HOST/telnyx/webhook"
