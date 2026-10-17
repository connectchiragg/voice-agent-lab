#!/usr/bin/env bash
# One deploy of the merged branch to the VM, run from the Mac (repo root or anywhere in it).
# Builds on the VM with BuildKit (docker buildx bake, both images in one run). A code-only
# change rebuilds only the small COPY layers: the whole deploy is about 1-1.5 minutes.
#
#   bash deploy/deploy-all.sh            # deploys the branch valr-interviews
#   BRANCH=<branch> bash deploy/deploy-all.sh
#   bash deploy/deploy-all.sh --dry-run  # prints the steps, runs nothing
#   DRY_RUN=1 bash deploy/deploy-all.sh  # ships and builds, stops before the swap
#
# Needs docker buildx on the VM, once (the Dockerfile's pip cache mounts need BuildKit):
#   ssh -i ~/.ssh/voice-agent-lab_ed25519 valr-vm 'sh -s' < deploy/vm-buildx.sh The steps after the build are shared (deploy/deploy-lib.sh) with
# deploy/deploy-pull.sh, which pulls images built by GitHub Actions instead.
#
# Steps, stopping at the first failure:
#  0. Checks that the VM has docker buildx, and its free disk: under PRUNE_BELOW_GB (6) it
#     prunes old rollback tags and build cache first, under ABORT_BELOW_GB (4) it stops.
#  1. Tags the images the running containers use as :rollback-<stamp> (by image id, so a
#     retagged :latest, such as an unreleased build, does not matter).
#  2. Ships `git archive $BRANCH` to ~/voice-agent-lab.next with the .env and .env.research of
#     the live tree (~/voice-agent-lab, else the newest ~/voice-agent-lab.pre-* that has them).
#  3. Builds the app and worker images there in one `docker buildx bake` (docker-bake.hcl),
#     nice'd, while the live containers keep serving; tags them :next and :<commit>.
#  4. Waits (up to WAIT_S, default 900) until no call is live: no established connection to
#     the app's port 7860 inside its container, twice, 10 s apart. Else stops, changing nothing.
#  5. Makes .next the live tree (the old one becomes ~/voice-agent-lab.prev-<stamp>), makes
#     sure the worker's writable data dir exists (deploy/valr-data-live.sh setup), swaps
#     the app container and waits for it to be healthy (rolls the app back if it is not).
#  6. Runs deploy/research-setup.sh (voice-research) on the image from step 3 (SKIP_BUILD=1);
#     restarts voice-callbacks only if its code (app/callback_worker.py,
#     app/callback_workflow.py) or the app's packages changed.
#  7. Checks: app healthy, /healthz ok, the worker's "VALR LLM gateway" and "research worker
#     on" log lines, no "BYOK ALERT". Then retention (only after success): keeps the
#     rollback tags of the 2 newest deploys, the 2 newest .prev-*/.bad-* trees, build cache
#     under 3 GB, and removes dangling images. Prints the rollback commands.
# Never touches cloudflared, Temporal or Postgres. Secrets are never printed.
set -euo pipefail
VM=${VM:-valr-vm}
KEY=${KEY:-$HOME/.ssh/voice-agent-lab_ed25519}
BRANCH=${BRANCH:-valr-interviews}
WAIT_S=${WAIT_S:-900}
STAMP=$(date -u +%Y%m%d-%H%M)
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
COMMIT=$(git -C "$REPO" rev-parse --short "$BRANCH")
# shellcheck source=deploy/deploy-lib.sh
. "$REPO/deploy/deploy-lib.sh"
parse_flags "$@"
trap ssh_close EXIT

say "Deploying $BRANCH at $COMMIT to $VM (stamp $STAMP)"
git -C "$REPO" cat-file -e "$BRANCH:docker-bake.hcl" 2>/dev/null \
  || { echo "$BRANCH has no docker-bake.hcl: merge valr-fastdeploy into it first." >&2; exit 1; }
ssh_vm 'bash -s' <<'REMOTE'
# check that docker buildx (BuildKit) is installed
sudo docker buildx version >/dev/null 2>&1 && { echo "buildx: $(sudo docker buildx version | cut -d' ' -f2)"; exit 0; }
echo "The VM has no docker buildx. Install it once, from the Mac (repo root):" >&2
echo "  ssh -i ~/.ssh/voice-agent-lab_ed25519 valr-vm 'sh -s' < deploy/vm-buildx.sh" >&2
echo "Nothing changed." >&2
exit 1
REMOTE
step_disk_preflight
step_rollback_tags
step_ship_source

say "Build the app and worker images on the VM (one buildx bake, nice'd; the live app keeps serving)"
ssh_vm "COMMIT=$COMMIT bash -s" <<'REMOTE'
# docker buildx bake in ~/voice-agent-lab.next: voice-agent-lab(-worker):next and :<commit>
set -euo pipefail
cd ~/voice-agent-lab.next
echo "load before: $(cut -d' ' -f1-3 /proc/loadavg)"
start=$(date +%s)
sudo nice -n 10 docker buildx bake --progress=quiet
for repo in voice-agent-lab voice-agent-lab-worker; do sudo docker tag "$repo:next" "$repo:$COMMIT"; done
echo "built both images in $(( $(date +%s) - start ))s; load after: $(cut -d' ' -f1-3 /proc/loadavg)"
REMOTE

steps_go_live
