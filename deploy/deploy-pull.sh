#!/usr/bin/env bash
# OPTIONAL deploy without building on the VM: the images come from GHCR, built by
# .github/workflows/images.yml (manual "Run workflow" on the commit first). Run from the Mac:
#
#   bash deploy/deploy-pull.sh            # deploys the branch valr-interviews
#   BRANCH=<branch> bash deploy/deploy-pull.sh
#   bash deploy/deploy-pull.sh --dry-run  # prints the steps, runs nothing
#
# One-time on the VM: a GHCR read token (a GitHub personal access token with only
# read:packages), entered by the owner, never stored in this repo:
#   sudo docker login ghcr.io -u <github-user>     # paste the token at the prompt
#
# Steps: rollback tags, ship the source tree (env files stay on the VM), docker pull both
# images by full commit sha (only missing layers download) and tag them :next and :<commit>,
# then the same safe steps as deploy-all.sh (deploy/deploy-lib.sh).
set -euo pipefail
VM=${VM:-valr-vm}
KEY=${KEY:-$HOME/.ssh/voice-agent-lab_ed25519}
BRANCH=${BRANCH:-valr-interviews}
WAIT_S=${WAIT_S:-900}
GHCR=${GHCR:-ghcr.io/connectchiragg}
STAMP=$(date -u +%Y%m%d-%H%M)
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
COMMIT=$(git -C "$REPO" rev-parse --short "$BRANCH")
SHA=$(git -C "$REPO" rev-parse "$BRANCH")
# shellcheck source=deploy/deploy-lib.sh
. "$REPO/deploy/deploy-lib.sh"
parse_flags "$@"
trap ssh_close EXIT

say "Deploying $BRANCH at $COMMIT to $VM (stamp $STAMP), images from $GHCR"
step_disk_preflight
step_rollback_tags
step_ship_source

say "Pull the images built by GitHub Actions"
ssh_vm "GHCR=$GHCR SHA=$SHA COMMIT=$COMMIT bash -s" <<'REMOTE'
# docker pull voice-agent-lab(-worker):<sha> from GHCR, tag them :next and :<commit>
set -euo pipefail
for repo in voice-agent-lab voice-agent-lab-worker; do
  sudo docker pull -q "$GHCR/$repo:$SHA" >/dev/null \
    || { echo "No $GHCR/$repo:$SHA: run the images workflow on this commit (or sudo docker login ghcr.io). Nothing live changed." >&2; exit 1; }
  sudo docker tag "$GHCR/$repo:$SHA" "$repo:next"
  sudo docker tag "$GHCR/$repo:$SHA" "$repo:$COMMIT"
  echo "$repo:$COMMIT pulled"
done
REMOTE

steps_go_live
