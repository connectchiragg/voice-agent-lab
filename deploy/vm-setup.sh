#!/bin/sh
# Build and run the voice agent on a fresh Ubuntu VM (Oracle A1 arm64 or AWS x86_64),
# then expose it over HTTPS/WSS with a Cloudflare quick tunnel (no Cloudflare account).
# Run from the unpacked source directory, with .env next to this repo's Dockerfile:
#   sh deploy/vm-setup.sh
set -eu
cd "$(dirname "$0")/.."
[ -s .env ] || { echo "Create .env with GROQ_API_KEY and DEEPGRAM_API_KEY first (see docs/deploy.md)." >&2; exit 1; }

if ! command -v docker >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y docker.io
fi

sudo docker build -t voice-agent-lab .
sudo docker rm -f voice-agent-lab cloudflared 2>/dev/null || true
# Bound to localhost only: the tunnel is the sole public entry point.
sudo docker run -d --name voice-agent-lab --restart unless-stopped \
  --env-file .env -p 127.0.0.1:7860:7860 voice-agent-lab
sudo docker run -d --name cloudflared --restart unless-stopped --network host \
  cloudflare/cloudflared:2026.9.3 tunnel --no-autoupdate --url http://127.0.0.1:7860

echo "Waiting for the app and the tunnel URL..."
for _ in $(seq 60); do
  url=$(sudo docker logs cloudflared 2>&1 | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | head -1 || true)
  if [ -n "$url" ] && curl -fsS http://127.0.0.1:7860/healthz >/dev/null 2>&1; then
    echo "Ready: $url"
    exit 0
  fi
  sleep 5
done
echo "Not ready after 5 minutes. Check: sudo docker logs voice-agent-lab; sudo docker logs cloudflared" >&2
exit 1
