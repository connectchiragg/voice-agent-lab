#!/bin/sh
# One-time, on the VM: installs docker buildx so builds there use BuildKit (no "legacy
# builder is deprecated" warning; only the stages a target needs; pip cache mounts).
# deploy/deploy-all.sh and deploy/research-setup.sh need it. Idempotent: does nothing when
# buildx is already there. From the Mac (repo root):
#   ssh -i ~/.ssh/voice-agent-lab_ed25519 ubuntu@<vm-ip> 'sh -s' < deploy/vm-buildx.sh
# or on the VM: sh deploy/vm-buildx.sh
# The first BuildKit build reinstalls the Python packages once (the legacy builder's cache
# is not shared with BuildKit); later builds are cached again.
set -eu
if sudo docker buildx version >/dev/null 2>&1; then
  echo "docker buildx already installed: $(sudo docker buildx version)"; exit 0
fi
if dpkg -s docker-ce >/dev/null 2>&1; then
  pkg=docker-buildx-plugin   # Docker's own apt repository
else
  pkg=docker-buildx          # Ubuntu's docker.io (what deploy/vm-setup.sh installs)
fi
sudo apt-get update -q
sudo apt-get install -y -q "$pkg"
sudo docker buildx version
