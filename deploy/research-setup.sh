#!/bin/sh
# VALR research worker (README, Research callbacks): builds the Dockerfile's `worker`
# target (Python 3.13 with crewai) and (re)starts the `voice-research` container on the
# `voice-agent-lab` network. Needs Temporal and Postgres running (deploy/temporal-setup.sh)
# and ANTHROPIC_API_KEY in .env.research (chmod 600), which only this container reads.
# /data (read-only) and /data-live (writable: performance.json) come from
# deploy/valr-data-live.sh. Safe to run again. Run from the source directory:
#   sh deploy/research-setup.sh
#   SKIP_BUILD=1 sh deploy/research-setup.sh   # use $IMAGE as it is (deploy-all.sh built it)
#   IMAGE=voice-agent-lab-worker:<tag> ...     # default voice-agent-lab-worker:latest
set -eu
cd "$(dirname "$0")/.."
DOCKER=${DOCKER:-sudo docker}
IMAGE=${IMAGE:-voice-agent-lab-worker:latest}
[ -s .env ] || { echo "Run from the source directory, next to .env." >&2; exit 1; }
grep -q '^ANTHROPIC_API_KEY=' .env.research 2>/dev/null \
  || { echo "Put ANTHROPIC_API_KEY=... in .env.research (chmod 600) first." >&2; exit 1; }
chmod 600 .env.research
if [ "${SKIP_BUILD:-0}" = 1 ]; then
  $DOCKER image inspect "$IMAGE" >/dev/null 2>&1 || { echo "SKIP_BUILD=1 but there is no image $IMAGE." >&2; exit 1; }
  echo "Using $IMAGE as it is (SKIP_BUILD=1)"
elif $DOCKER buildx version >/dev/null 2>&1; then
  $DOCKER buildx build --target worker -t "$IMAGE" .   # BuildKit
else
  echo "docker buildx is missing (the Dockerfile needs BuildKit): sh deploy/vm-buildx.sh" >&2; exit 1
fi
$DOCKER rm -f voice-research >/dev/null 2>&1 || true
# The research worker's RAM is capped so a runaway job cannot starve the live calls.
$DOCKER run -d --name voice-research --restart unless-stopped --network voice-agent-lab \
  --env-file .env --env-file .env.research --memory 768m --no-healthcheck \
  $(sh deploy/valr-data-live.sh worker) "$IMAGE"
sleep 5
$DOCKER logs voice-research 2>&1 | tail -3
