#!/usr/bin/env bash
# Turns the Gmail lead tracker on and deploys, run by the owner from the Mac at the repo root:
#
#   bash deploy/enable-leads-and-deploy.sh
#   BRANCH=<branch> bash deploy/enable-leads-and-deploy.sh   # passed on to deploy-all.sh
#
# 1. Sets VALR_LEADS=1 and the four Gmail push settings in the VM's ~/voice-agent-lab/.env
#    (replaces the line or appends it). Prints only the key names, never values or the file.
#    The owner's name now comes only from settings (no default in the code): if VALR_OWNER_NAME
#    or VALR_CANDIDATE_NAME is missing there, the script asks for it once and stores it.
# 2. Creates and seeds the worker's writable data dir /srv/valr-data-live
#    (deploy/valr-data-live.sh setup: uid 1000, cp -n from /srv/valr-data).
# 3. Runs deploy/deploy-all.sh (which stops at its first failure).
# 4. Prints checks: /healthz, the worker's lead/Gmail/post-call log lines, the Temporal
#    schedules valr-gmail-poll and valr-gmail-watch, and the lead counts (backfill).
# Never touches cloudflared, Temporal's or Postgres' containers, or GCP.
set -euo pipefail
VM=${VM:-ubuntu@<vm-ip>}
KEY=${KEY:-$HOME/.ssh/voice-agent-lab_ed25519}
TEMPORAL_VERSION=${TEMPORAL_VERSION:-1.32.1}
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
ssh_vm() { ssh -i "$KEY" -o ConnectTimeout=15 -o BatchMode=yes "$VM" "$@"; }
say() { printf '\n== %s\n' "$*"; }
export VM KEY

say "1. Lead tracker settings in the VM .env"
# Not secrets, but still never echoed back: only "set"/"updated"/"unchanged" per key.
ssh_vm 'bash -s' <<'REMOTE'
set -euo pipefail
cd ~/voice-agent-lab
[ -s .env ] || { echo "No ~/voice-agent-lab/.env on the VM" >&2; exit 1; }
set_key() {
  local key=$1 value=$2 current t
  current=$(grep -E "^$key=" .env | tail -1 || true)
  if [ "$current" = "$key=$value" ]; then echo "$key: unchanged"; return; fi
  t=$(mktemp)
  awk -v k="$key" -v v="$value" 'BEGIN { done = 0 }
    index($0, k "=") == 1 { if (!done) { print k "=" v; done = 1 } next }
    { print }
    END { if (!done) print k "=" v }' .env > "$t"
  cat "$t" > .env && rm -f "$t"
  if [ -n "$current" ]; then echo "$key: updated"; else echo "$key: set"; fi
}
set_key VALR_LEADS 1
set_key VALR_GMAIL_TOPIC projects/framer-511620/topics/valr-gmail
set_key VALR_GMAIL_SUBSCRIPTION projects/framer-511620/subscriptions/valr-gmail-push
set_key VALR_GMAIL_PUSH_SA valr-gmail-push@framer-511620.iam.gserviceaccount.com
set_key VALR_GMAIL_PUSH_AUDIENCE https://valr.haciensus.com/hooks/gmail
chmod 600 .env
REMOTE

say "1b. Your name settings (greetings, and finding you in meeting transcripts)"
missing=$(ssh_vm 'cd ~/voice-agent-lab && for k in VALR_OWNER_NAME VALR_CANDIDATE_NAME; do grep -qE "^$k=.+" .env || echo "$k"; done')
for key in $missing; do
  case $key in
    VALR_OWNER_NAME) prompt="Your first name, as VALR greets you (VALR_OWNER_NAME): " ;;
    *) prompt="Your name as it appears in Fathom/Fireflies recordings (VALR_CANDIDATE_NAME; Enter = same as above): " ;;
  esac
  read -r -p "$prompt" value < /dev/tty
  [ -n "$value" ] || [ "$key" = VALR_OWNER_NAME ] || value=${owner_name:-}
  [ "$key" = VALR_OWNER_NAME ] && owner_name=$value
  if [ -z "$value" ]; then echo "$key: left unset"; continue; fi
  # The value travels on stdin, never on a command line or in the output.
  printf '%s\n' "$value" | ssh_vm "KEY_NAME=$key bash -c 'cd ~/voice-agent-lab && IFS= read -r v && t=\$(mktemp) && grep -vE \"^\$KEY_NAME=\" .env > \"\$t\" || true; printf \"%s=%s\\n\" \"\$KEY_NAME\" \"\$v\" >> \"\$t\" && cat \"\$t\" > .env && rm -f \"\$t\" && chmod 600 .env && echo \"\$KEY_NAME: set\"'"
done
[ -n "$missing" ] || echo "VALR_OWNER_NAME, VALR_CANDIDATE_NAME: already set"

say "2. Worker data dir /srv/valr-data-live"
ssh_vm 'sudo sh -s setup' < "$REPO/deploy/valr-data-live.sh"

say "3. Deploy (deploy/deploy-all.sh)"
bash "$REPO/deploy/deploy-all.sh"

say "4. Checks"
ssh_vm "TEMPORAL_VERSION=$TEMPORAL_VERSION bash -s" <<'REMOTE'
set -uo pipefail
sleep 20
health=$(sudo docker exec voice-agent-lab python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=4).read().decode())" 2>&1)
echo "healthz: $health"
echo "-- mounts: voice-research"
sudo docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}} (rw={{.RW}}){{"\n"}}{{end}}' voice-research
echo "-- worker log (last 10 min): lead / gmail / post-call / performance"
sudo docker logs --since 10m voice-research 2>&1 | grep -iE 'lead|gmail|post-call|performance' | tail -n 20 || echo "(no matching lines yet)"
admin="sudo docker run --rm --network voice-agent-lab temporalio/admin-tools:$TEMPORAL_VERSION temporal --address temporal:7233 -n voice-agent-lab"
for schedule in valr-gmail-poll valr-gmail-watch; do
  echo "-- Temporal schedule $schedule"
  $admin schedule describe --schedule-id "$schedule" 2>&1 | head -n 30 || echo "MISSING: $schedule"
done
echo "-- lead counts (backfill: decisions_30d = messages seen/kept, leads = created)"
sudo docker exec voice-research python -m app.lead_worker stats 2>&1 | tail -n 40
echo
echo "The 30-day backfill can take several minutes; re-run the counts with:"
echo "  sudo docker exec voice-research python -m app.lead_worker stats"
REMOTE
