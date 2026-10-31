#!/usr/bin/env bash
# The multi-tenant release (app/tenant.py), run once from the Mac (repo root), after `main` holds it:
#
#   bash deploy/migrate-multitenant.sh            # checks, backs up, deploys, verifies
#   bash deploy/migrate-multitenant.sh --check    # the read-only checks only
#
# The release changes the database the first time the new code touches each table: owner_id
# on every per-user table, keys widened to (owner_id, key), row-level security, and the role
# valr_app. The old code cannot run on the migrated tables, so the rollback is the image
# rollback that deploy-all.sh prints PLUS the restore of the dump taken here.
#
# Steps, stopping at the first failure:
#  1. Checks: the app's Postgres role is a superuser (it creates valr_app), VALR_TOKEN_KEY is
#     set (it seals users' API keys), free disk.
#  2. pg_dump -Fc of voice_agent to /srv/valr-backups/pre-multitenant-<stamp>.dump (kept).
#  3. deploy/deploy-all.sh (BRANCH=main).
#  4. Verifies: valr_app exists, row-level security is on, and the owner's rows are all "owner".
#
# Restore (after the image rollback):
#   ssh valr-vm 'sudo docker exec -i voice-postgres pg_restore -U voice_agent -d voice_agent --clean --if-exists' \
#     < /dev/null  # with the dump: sudo docker exec -i ... < /srv/valr-backups/pre-multitenant-<stamp>.dump
set -euo pipefail
VM=${VM:-valr-vm}
KEY=${KEY:-$HOME/.ssh/voice-agent-lab_ed25519}
STAMP=$(date -u +%Y%m%d-%H%M)
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
vm() { ssh -o ConnectTimeout=10 -i "$KEY" "$VM" "$@"; }
psql_vm() { vm "sudo docker exec voice-postgres psql -U voice_agent -d voice_agent -Atc \"$1\""; }

echo "== Checks"
[ "$(psql_vm 'select rolsuper from pg_roles where rolname = current_user')" = "t" ] \
  || { echo "The app's role is not a superuser: create valr_app by hand first (see app/tenant.py)." >&2; exit 1; }
vm 'grep -q "^VALR_TOKEN_KEY=." ~/voice-agent-lab/.env' || { echo "VALR_TOKEN_KEY is not set in .env" >&2; exit 1; }
vm 'df -h / | tail -1'
psql_vm "select pg_size_pretty(pg_database_size('voice_agent'))"
[ "${1:-}" = "--check" ] && exit 0

echo "== Backup"
vm "sudo mkdir -p /srv/valr-backups && sudo docker exec voice-postgres pg_dump -U voice_agent -Fc voice_agent \
    | sudo tee /srv/valr-backups/pre-multitenant-$STAMP.dump >/dev/null && ls -lh /srv/valr-backups/pre-multitenant-$STAMP.dump"

echo "== Deploy"
BRANCH=main bash "$REPO/deploy/deploy-all.sh"

echo "== Verify"
curl -fsS https://valr.haciensus.com/healthz >/dev/null && echo "healthz ok"
curl -fsS -o /dev/null https://valr.haciensus.com/login && echo "login page ok"
psql_vm "select 'valr_app', count(*) from pg_roles where rolname = 'valr_app'"
psql_vm "select 'tables with row-level security', count(*) from pg_class where relrowsecurity"
psql_vm "select 'owner_profile rows', owner_id, count(*) from owner_profile group by owner_id"
echo "Backup: /srv/valr-backups/pre-multitenant-$STAMP.dump"
