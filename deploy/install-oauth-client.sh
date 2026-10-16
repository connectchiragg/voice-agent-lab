#!/bin/sh
# Copies the Google OAuth client id/secret from the downloaded JSON into the VM .env
# without printing them, then restarts the app and the research worker.
set -eu
f=$(ls -t "$HOME"/Downloads/client_secret_*.json 2>/dev/null | head -1)
[ -n "$f" ] || { echo "no client_secret_*.json in ~/Downloads"; exit 1; }
python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
w = d.get("web") or d.get("installed") or {}
assert w.get("client_id") and w.get("client_secret"), "missing fields"
assert "https://valr.haciensus.com/oauth/google/callback" in (w.get("redirect_uris") or []), "redirect URI missing"
print("GOOGLE_OAUTH_CLIENT_ID=" + w["client_id"])
print("GOOGLE_OAUTH_CLIENT_SECRET=" + w["client_secret"])
' "$f" | ssh -i "$HOME/.ssh/voice-agent-lab_ed25519" -o BatchMode=yes ubuntu@<vm-ip> '
cd voice-agent-lab && t=$(mktemp) && grep -vE "^GOOGLE_OAUTH_CLIENT_(ID|SECRET)=" .env > "$t" && cat >> "$t" && cat "$t" > .env && rm -f "$t" && chmod 600 .env
grep -cE "^GOOGLE_OAUTH_CLIENT_(ID|SECRET)=" .env
sudo -n docker rm -f voice-agent-lab >/dev/null && sudo -n docker run -d --name voice-agent-lab --restart unless-stopped --env-file .env --network voice-agent-lab $(sh deploy/valr-data-live.sh app) -p 127.0.0.1:7860:7860 voice-agent-lab:latest >/dev/null
DOCKER="sudo -n docker" sh deploy/research-setup.sh >/dev/null 2>&1
sleep 8; curl -s -o /dev/null -w "%{http_code}\n" localhost:7860/healthz'
echo installed
