#!/usr/bin/env bash
# One deploy of the merged branch to the VM, run from the Mac (repo root or anywhere in it):
#
#   bash deploy/deploy-all.sh            # deploys the branch valr-interviews
#   BRANCH=<branch> bash deploy/deploy-all.sh
#   DRY_RUN=1 bash deploy/deploy-all.sh  # steps 1-4 only: ships and builds, changes nothing live
#
# Steps, stopping at the first failure:
#  1. Tags the images the running containers use as :rollback-<stamp> (by image id, so a
#     retagged :latest, such as an unreleased build, does not matter).
#  2. Ships `git archive $BRANCH` to ~/voice-agent-lab.next with the .env and .env.research of
#     the live tree (~/voice-agent-lab, else the newest ~/voice-agent-lab.pre-* that has them).
#  3. Builds the app image and the worker image there (nice'd; dependency layers are cached
#     when the lock files are unchanged).
#  4. Waits (up to WAIT_S, default 900) until no call is live: no established connection to
#     the app's port 7860 inside its container, twice, 10 s apart. Else stops, changing nothing.
#  5. Makes .next the live tree (the old one becomes ~/voice-agent-lab.prev-<stamp>), swaps
#     the app container and waits for it to be healthy (rolls the app back if it is not).
#  6. Runs deploy/research-setup.sh (voice-research); restarts voice-callbacks only if its
#     code (app/callback_worker.py, app/callback_workflow.py) or the app's packages changed.
#  7. Checks: app healthy, /healthz ok, the worker's "VALR LLM gateway" and "research worker
#     on" log lines, no "BYOK ALERT". Prints the rollback commands.
# Never touches cloudflared, Temporal or Postgres. Secrets are never printed.
set -euo pipefail
VM=${VM:-ubuntu@<vm-ip>}
KEY=${KEY:-$HOME/.ssh/voice-agent-lab_ed25519}
BRANCH=${BRANCH:-valr-interviews}
WAIT_S=${WAIT_S:-900}
STAMP=$(date -u +%Y%m%d-%H%M)
REPO=$(git rev-parse --show-toplevel)
COMMIT=$(git -C "$REPO" rev-parse --short "$BRANCH")
ssh_vm() { ssh -i "$KEY" -o ConnectTimeout=15 -o BatchMode=yes "$VM" "$@"; }
say() { printf '\n== %s\n' "$*"; }

say "Deploying $BRANCH at $COMMIT to $VM (stamp $STAMP)"

say "1. Rollback tags for the running images"
ssh_vm "STAMP=$STAMP bash -s" <<'REMOTE'
set -euo pipefail
for pair in voice-agent-lab:voice-agent-lab voice-research:voice-agent-lab-worker voice-callbacks:voice-agent-lab; do
  container=${pair%%:*}; repo=${pair#*:}
  image=$(sudo docker inspect -f '{{.Image}}' "$container")
  sudo docker tag "$image" "$repo:rollback-$STAMP-$container"
  echo "$container: $repo:rollback-$STAMP-$container (${image:7:12})"
done
REMOTE

say "2. Ship $BRANCH to ~/voice-agent-lab.next"
git -C "$REPO" archive "$BRANCH" | ssh_vm "cat > /tmp/valr-deploy-$STAMP.tar"
ssh_vm "STAMP=$STAMP COMMIT=$COMMIT bash -s" <<'REMOTE'
set -euo pipefail
rm -rf ~/voice-agent-lab.next && mkdir ~/voice-agent-lab.next
tar -x -C ~/voice-agent-lab.next -f /tmp/valr-deploy-$STAMP.tar && rm -f /tmp/valr-deploy-$STAMP.tar
# The live tree may have been swapped by an earlier, unfinished deploy: take the env files
# from ~/voice-agent-lab, else from the newest earlier tree that has both.
src=""
for dir in ~/voice-agent-lab $(ls -dt ~/voice-agent-lab.pre-* ~/voice-agent-lab.prev-* 2>/dev/null); do
  if [ -s "$dir/.env" ] && [ -s "$dir/.env.research" ]; then src=$dir; break; fi
done
[ -n "$src" ] || { echo "No tree with .env and .env.research found" >&2; exit 1; }
cp -p "$src/.env" "$src/.env.research" ~/voice-agent-lab.next/
chmod 600 ~/voice-agent-lab.next/.env ~/voice-agent-lab.next/.env.research
echo "$COMMIT" > ~/voice-agent-lab.next/DEPLOYED_COMMIT
echo "env files from $src; keys in .env.research: $(grep -o -E '^(OPENROUTER|ANTHROPIC)_API_KEY=' ~/voice-agent-lab.next/.env.research | tr '\n' ' ')"
REMOTE

say "3. Build the app and worker images (nice'd)"
ssh_vm 'bash -s' <<'REMOTE'
set -euo pipefail
cd ~/voice-agent-lab.next
cat /proc/loadavg
time sudo nice -n 10 docker build -q -t voice-agent-lab:next .
time sudo nice -n 10 docker build -q --target worker -t voice-agent-lab-worker:next .
REMOTE

say "4. Wait for no live call (up to ${WAIT_S}s)"
ssh_vm "WAIT_S=$WAIT_S bash -s" <<'REMOTE'
set -euo pipefail
live() {
  pid=$(sudo docker inspect -f '{{.State.Pid}}' voice-agent-lab)
  sudo nsenter -t "$pid" -n ss -Htn state established '( sport = :7860 )' | wc -l
}
deadline=$(( $(date +%s) + WAIT_S ))
while :; do
  a=$(live); sleep 10; b=$(live)
  if [ "$a" = 0 ] && [ "$b" = 0 ]; then echo "No live call."; break; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Still $b connection(s) to the app after ${WAIT_S}s: not swapping. Nothing changed." >&2; exit 3
  fi
  echo "$b connection(s) to the app; waiting"; sleep 20
done
REMOTE

if [ "${DRY_RUN:-0}" = 1 ]; then
  say "DRY_RUN=1: stopping before the swap; nothing live changed (~/voice-agent-lab.next and the :next images are ready)"
  exit 0
fi

say "5. Swap the tree and the app"
ssh_vm "STAMP=$STAMP bash -s" <<'REMOTE'
set -euo pipefail
mv ~/voice-agent-lab ~/voice-agent-lab.prev-$STAMP
mv ~/voice-agent-lab.next ~/voice-agent-lab
cd ~/voice-agent-lab
sudo docker tag voice-agent-lab:next voice-agent-lab:latest
sudo docker tag voice-agent-lab-worker:next voice-agent-lab-worker:latest
run_app() {
  sudo docker rm -f voice-agent-lab >/dev/null
  sudo docker run -d --name voice-agent-lab --restart unless-stopped --env-file .env \
    --network voice-agent-lab -p 127.0.0.1:7860:7860 \
    $( [ -d /srv/valr-data ] && echo "-v /srv/valr-data:/data:ro" ) "$1" >/dev/null
}
run_app voice-agent-lab:latest
for _ in $(seq 1 24); do
  state=$(sudo docker inspect -f '{{.State.Health.Status}}' voice-agent-lab)
  [ "$state" = healthy ] && break
  sleep 5
done
echo "app: $state"
if [ "$state" != healthy ]; then
  echo "The app is not healthy: rolling the app back." >&2
  sudo docker logs --tail 20 voice-agent-lab 2>&1 | sed 's/^/  app> /' >&2
  run_app voice-agent-lab:rollback-$STAMP-voice-agent-lab
  exit 4
fi
REMOTE

say "6. voice-research (research-setup.sh) and voice-callbacks if changed"
ssh_vm "STAMP=$STAMP bash -s" <<'REMOTE'
set -euo pipefail
cd ~/voice-agent-lab
DOCKER="sudo nice -n 10 docker" sh deploy/research-setup.sh
changed=""
for file in app/callback_worker.py app/callback_workflow.py; do
  old=$(sudo docker exec voice-callbacks md5sum "/app/$file" 2>/dev/null | cut -d' ' -f1 || true)
  new=$(md5sum "$file" | cut -d' ' -f1)
  [ "$old" = "$new" ] || changed="$changed $file"
done
old_pkgs=$(sudo docker exec voice-callbacks pip freeze 2>/dev/null | md5sum | cut -d' ' -f1)
new_pkgs=$(sudo docker run --rm --entrypoint pip voice-agent-lab:latest freeze 2>/dev/null | md5sum | cut -d' ' -f1)
[ "$old_pkgs" = "$new_pkgs" ] || changed="$changed packages"
if [ -n "$changed" ]; then
  echo "voice-callbacks changed ($changed ): restarting it"
  sudo docker rm -f voice-callbacks >/dev/null
  sudo docker run -d --name voice-callbacks --restart unless-stopped --env-file .env \
    --network voice-agent-lab --no-healthcheck voice-agent-lab:latest python -m app.callback_worker >/dev/null
else
  echo "voice-callbacks unchanged: left running"
fi
REMOTE

say "7. Checks"
ssh_vm 'bash -s' <<'REMOTE'
set -uo pipefail
sleep 8
ok=1
sudo docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'voice-(agent-lab|research|callbacks)'
health=$(sudo docker exec voice-agent-lab python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=4).read().decode())" 2>&1)
echo "healthz: $health"; case "$health" in *'"ok":true'*) ;; *) ok=0 ;; esac
logs=$(sudo docker logs voice-research 2>&1)
echo "$logs" | grep -m1 -o 'VALR LLM gateway .*' || { echo "MISSING: the LLM gateway log line"; ok=0; }
echo "$logs" | grep -q 'VALR research worker on' && echo "worker: polling its task queue" || { echo "MISSING: research worker start"; ok=0; }
echo "$logs" | grep -q 'BYOK ALERT' && { echo "BYOK ALERT in the worker log"; ok=0; }
sudo docker logs voice-callbacks 2>&1 | tail -2
cat ~/voice-agent-lab/DEPLOYED_COMMIT
[ "$ok" = 1 ] && echo "ALL CHECKS PASSED" || { echo "SOME CHECKS FAILED"; exit 5; }
REMOTE

cat <<EOF

== Rollback (on the VM: ssh -i $KEY $VM), only what you need:
  # app
  sudo docker rm -f voice-agent-lab && sudo docker run -d --name voice-agent-lab --restart unless-stopped --env-file ~/voice-agent-lab.prev-$STAMP/.env --network voice-agent-lab -p 127.0.0.1:7860:7860 -v /srv/valr-data:/data:ro voice-agent-lab:rollback-$STAMP-voice-agent-lab
  # research worker
  sudo docker rm -f voice-research && sudo docker run -d --name voice-research --restart unless-stopped --network voice-agent-lab --env-file ~/voice-agent-lab.prev-$STAMP/.env --env-file ~/voice-agent-lab.prev-$STAMP/.env.research --memory 768m --no-healthcheck -v /srv/valr-data:/data:ro voice-agent-lab-worker:rollback-$STAMP-voice-research
  # callbacks (only if it was restarted)
  sudo docker rm -f voice-callbacks && sudo docker run -d --name voice-callbacks --restart unless-stopped --env-file ~/voice-agent-lab.prev-$STAMP/.env --network voice-agent-lab --no-healthcheck voice-agent-lab:rollback-$STAMP-voice-callbacks python -m app.callback_worker
  # source tree
  mv ~/voice-agent-lab ~/voice-agent-lab.bad-$STAMP && mv ~/voice-agent-lab.prev-$STAMP ~/voice-agent-lab
  # or only take background Claude off OpenRouter: add LLM_GATEWAY=anthropic to ~/voice-agent-lab/.env.research, then restart voice-research
EOF
