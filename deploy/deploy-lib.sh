# Shared deploy steps for deploy/deploy-all.sh (builds on the VM) and deploy/deploy-pull.sh
# (pulls images built by GitHub Actions). Sourced, not run. The caller sets VM, KEY, BRANCH, WAIT_S, STAMP, REPO,
# COMMIT and DRY (1 = --dry-run: print each step, run nothing locally or on the VM).
# Every remote script starts with a "# ..." line: --dry-run prints it instead of running it.

SSH_OPTS=(-i "$KEY" -o ConnectTimeout=15 -o BatchMode=yes
  -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/valr-deploy-%C" -o ControlPersist=120)

ssh_vm() {
  if [ "$DRY" = 1 ]; then
    local what; what=$(head -1 | sed 's/^# *//')
    printf '   [dry-run] on the VM (ssh %s %s): %s\n' "$VM" "$*" "$what"
    cat >/dev/null
  else
    ssh "${SSH_OPTS[@]}" "$VM" "$@"
  fi
}
# Run a local command, or print it under --dry-run.
run() { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else "$@"; fi; }
say() { printf '\n== %s\n' "$*"; }
ssh_close() { [ "$DRY" = 1 ] || ssh "${SSH_OPTS[@]}" -O exit "$VM" 2>/dev/null || true; }
parse_flags() { DRY=0; for arg in "$@"; do case "$arg" in --dry-run) DRY=1 ;; *) echo "unknown option: $arg" >&2; exit 2 ;; esac; done; }

step_rollback_tags() {
  say "Rollback tags for the running images"
  ssh_vm "STAMP=$STAMP bash -s" <<'REMOTE'
# tag the images of voice-agent-lab, voice-research, voice-callbacks as :rollback-<stamp>
set -euo pipefail
for pair in voice-agent-lab:voice-agent-lab voice-research:voice-agent-lab-worker voice-callbacks:voice-agent-lab; do
  container=${pair%%:*}; repo=${pair#*:}
  image=$(sudo docker inspect -f '{{.Image}}' "$container")
  sudo docker tag "$image" "$repo:rollback-$STAMP-$container"
  echo "$container: $repo:rollback-$STAMP-$container (${image:7:12})"
done
REMOTE
}

step_ship_source() {
  say "Ship $BRANCH to ~/voice-agent-lab.next (git archive; the env files stay on the VM)"
  if [ "$DRY" = 1 ]; then
    run "git archive $BRANCH | ssh $VM 'cat > /tmp/valr-deploy-$STAMP.tar'"
  else
    git -C "$REPO" archive "$BRANCH" | ssh_vm "cat > /tmp/valr-deploy-$STAMP.tar"
  fi
  ssh_vm "STAMP=$STAMP COMMIT=$COMMIT bash -s" <<'REMOTE'
# unpack into ~/voice-agent-lab.next and copy .env and .env.research from the live tree
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
}

step_wait_no_call() {
  say "Wait for no live call (up to ${WAIT_S}s)"
  ssh_vm "WAIT_S=$WAIT_S bash -s" <<'REMOTE'
# wait until no connection to the app's port 7860, twice 10 s apart (else stop, nothing changed)
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
}

step_swap_app() {
  say "Swap the tree and the app (rolls the app back if it is not healthy)"
  ssh_vm "STAMP=$STAMP bash -s" <<'REMOTE'
# make .next live, tag :next as :latest, replace voice-agent-lab, wait for healthy or roll back
set -euo pipefail
mv ~/voice-agent-lab ~/voice-agent-lab.prev-$STAMP
mv ~/voice-agent-lab.next ~/voice-agent-lab
cd ~/voice-agent-lab
# The worker's writable data dir (/srv/valr-data-live): created and seeded once, idempotent.
sudo sh deploy/valr-data-live.sh setup
sudo docker tag voice-agent-lab:next voice-agent-lab:latest
sudo docker tag voice-agent-lab-worker:next voice-agent-lab-worker:latest
run_app() {
  sudo docker rm -f voice-agent-lab >/dev/null
  sudo docker run -d --name voice-agent-lab --restart unless-stopped --env-file .env \
    --network voice-agent-lab -p 127.0.0.1:7860:7860 \
    $(sh deploy/valr-data-live.sh app) "$1" >/dev/null
}
# Healthy = Docker's HEALTHCHECK passed, or the same /healthz probe it runs passes now
# (Docker only runs its first check after 30 s; probing it directly saves that wait).
probe() {
  sudo docker exec voice-agent-lab python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=4)" >/dev/null 2>&1
}
run_app voice-agent-lab:latest
state=starting
for _ in $(seq 1 60); do
  state=$(sudo docker inspect -f '{{.State.Health.Status}}' voice-agent-lab)
  [ "$state" = healthy ] && break
  [ "$state" != unhealthy ] && probe && { state=healthy; break; }
  sleep 2
done
echo "app: $state"
if [ "$state" != healthy ]; then
  echo "The app is not healthy: rolling the app back." >&2
  sudo docker logs --tail 20 voice-agent-lab 2>&1 | sed 's/^/  app> /' >&2
  run_app voice-agent-lab:rollback-$STAMP-voice-agent-lab
  exit 4
fi
REMOTE
}

step_worker_and_callbacks() {
  say "voice-research (research-setup.sh, using the image already built) and voice-callbacks if changed"
  ssh_vm "STAMP=$STAMP bash -s" <<'REMOTE'
# restart voice-research on voice-agent-lab-worker:latest; restart voice-callbacks if its code or packages changed
set -euo pipefail
cd ~/voice-agent-lab
SKIP_BUILD=1 DOCKER="sudo nice -n 10 docker" sh deploy/research-setup.sh
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
}

step_checks() {
  say "Checks"
  ssh_vm 'bash -s' <<'REMOTE'
# check: containers up, /healthz ok, the worker's gateway and start log lines, no BYOK ALERT
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
}

# Disk retention on the VM (a full disk once failed a build: 52 rollback tags, build cache,
# old trees). Thresholds in GB, from the Mac's environment.
PRUNE_BELOW_GB=${PRUNE_BELOW_GB:-6}
ABORT_BELOW_GB=${ABORT_BELOW_GB:-4}
KEEP_STAMPS=${KEEP_STAMPS:-2}       # rollback stamps (deploys) whose tags are kept
KEEP_TREES=${KEEP_TREES:-2}         # newest ~/voice-agent-lab.prev-* and .bad-* trees kept
BUILD_CACHE_MAX=${BUILD_CACHE_MAX:-3gb}

# Bash functions sent ahead of the retention scripts below.
VM_PRUNE_FN=''
IFS= read -r -d '' VM_PRUNE_FN <<'FN' || true  # (not $(cat): bash 3.2 misparses case in it)
gb_free() { sudo df -Pk /var/lib/docker | awk 'NR==2 {printf "%.1f", $4/1048576}'; }
below() { awk -v a="$1" -v b="$2" 'BEGIN {exit !(a < b)}'; }
untag() {  # untag <tag>: removes the image too when nothing else uses it
  if sudo docker rmi "$1" >/dev/null 2>&1; then removed=$((removed + 1)); else echo "  kept $1 (in use)"; fi
}
prune_images() {
  local tags keep stamp t tag cur removed=0
  # Rollback tags (repo:rollback-<YYYYMMDD-HHMM>-<container>): keep the KEEP_STAMPS newest stamps.
  tags=$(sudo docker images --format '{{.Repository}}:{{.Tag}}' | grep -E ':rollback-[0-9]{8}-[0-9]{4}-' || true)
  keep=$(printf '%s\n' "$tags" | sed -nE 's/.*:rollback-([0-9]{8}-[0-9]{4})-.*/\1/p' | sort -u | tail -n "$KEEP_STAMPS")
  for t in $tags; do
    stamp=$(printf '%s' "$t" | sed -E 's/.*:rollback-([0-9]{8}-[0-9]{4})-.*/\1/')
    printf '%s\n' "$keep" | grep -qx "$stamp" || untag "$t"
  done
  echo "rollback tags: removed $removed; kept stamps: $(echo $keep)"
  # Per-commit tags (voice-agent-lab(-worker):<sha>, also from GHCR): keep the live commit's only.
  removed=0
  cur=$(cat ~/voice-agent-lab/DEPLOYED_COMMIT 2>/dev/null || echo none)
  for t in $(sudo docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '(^|/)voice-agent-lab(-worker)?:[0-9a-f]{7,40}$' || true); do
    tag=${t##*:}
    case "$tag" in "$cur"*) ;; *) untag "$t" ;; esac
  done
  echo "commit tags: removed $removed (kept $cur)"
  echo "dangling images: $(sudo docker image prune -f | tail -1)"
  sudo docker buildx prune -f --max-used-space "$BUILD_CACHE_MAX" >/dev/null 2>&1 \
    || sudo docker builder prune -f --keep-storage "$BUILD_CACHE_MAX" >/dev/null 2>&1 \
    || echo "WARNING: build cache prune failed"
  echo "build cache now: $(sudo docker system df --format '{{.Type}} {{.Size}}' | sed -n 's/^Build Cache //p') (cap $BUILD_CACHE_MAX)"
}
FN
vm_prune() {  # vm_prune <env assignments> < script: sends the functions, then the script
  local script; script=$(cat)
  { printf '%s\n' "$(printf '%s\n' "$script" | head -1)" "set -euo pipefail" "$VM_PRUNE_FN";
    printf '%s\n' "$script"; } \
    | ssh_vm "KEEP_STAMPS=$KEEP_STAMPS KEEP_TREES=$KEEP_TREES BUILD_CACHE_MAX=$BUILD_CACHE_MAX PRUNE_BELOW_GB=$PRUNE_BELOW_GB ABORT_BELOW_GB=$ABORT_BELOW_GB bash -s"
}

# Before building or pulling: below PRUNE_BELOW_GB free, prune; still below ABORT_BELOW_GB, stop.
step_disk_preflight() {
  say "Disk space on the VM (prune below ${PRUNE_BELOW_GB} GB free, stop below ${ABORT_BELOW_GB} GB)"
  vm_prune <<'REMOTE'
# check free disk space; prune old rollback tags and build cache if low; stop if still too low
free=$(gb_free); echo "free: $free GB"
if below "$free" "$PRUNE_BELOW_GB"; then
  echo "under $PRUNE_BELOW_GB GB: pruning first"
  prune_images
  free=$(gb_free); echo "free after pruning: $free GB"
fi
if below "$free" "$ABORT_BELOW_GB"; then
  echo "Only $free GB free on the VM (need $ABORT_BELOW_GB GB to build): stopping, nothing live changed." >&2
  echo "See: sudo docker system df; ls -d ~/voice-agent-lab.*; sudo docker images | grep rollback" >&2
  exit 6
fi
REMOTE
}

# After a successful deploy only: old rollback/commit tags, dangling images, build cache, old trees.
step_retention() {
  say "Retention: keep ${KEEP_STAMPS} rollback stamps and ${KEEP_TREES} old trees of each kind, build cache under ${BUILD_CACHE_MAX}"
  vm_prune <<'REMOTE'
# prune old rollback and commit tags, dangling images, build cache over the cap, old .prev-*/.bad-* trees
echo "free before: $(gb_free) GB"
prune_images
for kind in prev bad; do
  for dir in $(ls -dt ~/voice-agent-lab."$kind"-* 2>/dev/null | tail -n +$((KEEP_TREES + 1))); do
    rm -rf "$dir" 2>/dev/null || sudo rm -rf "$dir"
    echo "removed $dir"
  done
done
echo "free after: $(gb_free) GB"
REMOTE
}

# Steps after the images exist on the VM as :next. DRY_RUN=1 (env) stops before the swap.
steps_go_live() {
  step_wait_no_call
  if [ "${DRY_RUN:-0}" = 1 ]; then
    say "DRY_RUN=1: stopping before the swap; nothing live changed (~/voice-agent-lab.next and the :next images are ready)"
    exit 0
  fi
  step_swap_app
  step_worker_and_callbacks
  step_checks
  step_retention
  print_rollback
}

print_rollback() {
  cat <<EOF

== Rollback (on the VM: ssh -i $KEY $VM), only what you need:
  # app
  sudo docker rm -f voice-agent-lab && sudo docker run -d --name voice-agent-lab --restart unless-stopped --env-file ~/voice-agent-lab.prev-$STAMP/.env --network voice-agent-lab -p 127.0.0.1:7860:7860 -v /srv/valr-data:/data:ro -v /srv/valr-data-live:/data-live:ro -e VALR_PERFORMANCE_PATH=/data-live/performance.json -e VALR_PERFORMANCE_CSV=/data-live/performance_scores.csv voice-agent-lab:rollback-$STAMP-voice-agent-lab
  # research worker
  sudo docker rm -f voice-research && sudo docker run -d --name voice-research --restart unless-stopped --network voice-agent-lab --env-file ~/voice-agent-lab.prev-$STAMP/.env --env-file ~/voice-agent-lab.prev-$STAMP/.env.research --memory 768m --no-healthcheck -v /srv/valr-data:/data:ro -v /srv/valr-data-live:/data-live -e VALR_PERFORMANCE_PATH=/data-live/performance.json -e VALR_PERFORMANCE_CSV=/data-live/performance_scores.csv -e VALR_PERFORMANCE_FALLBACK=/data/performance.json voice-agent-lab-worker:rollback-$STAMP-voice-research
  # callbacks (only if it was restarted)
  sudo docker rm -f voice-callbacks && sudo docker run -d --name voice-callbacks --restart unless-stopped --env-file ~/voice-agent-lab.prev-$STAMP/.env --network voice-agent-lab --no-healthcheck voice-agent-lab:rollback-$STAMP-voice-callbacks python -m app.callback_worker
  # source tree
  mv ~/voice-agent-lab ~/voice-agent-lab.bad-$STAMP && mv ~/voice-agent-lab.prev-$STAMP ~/voice-agent-lab
  # or only take background Claude off OpenRouter: add LLM_GATEWAY=anthropic to ~/voice-agent-lab/.env.research, then restart voice-research
EOF
}
