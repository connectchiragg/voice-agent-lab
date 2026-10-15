#!/bin/sh
# Self-hosted Temporal for scheduled callbacks, on the VM beside the app (docs/deploy.md).
# Needs the Postgres container `voice-postgres` on the `voice-agent-lab` network, and
# the app image built (deploy/vm-setup.sh). Safe to run again; it replaces only the
# temporal, temporal-ui and voice-callbacks containers. Run from the source directory:
#   sh deploy/temporal-setup.sh
set -eu
cd "$(dirname "$0")/.."
DOCKER=${DOCKER:-sudo docker}
NETWORK=voice-agent-lab
POSTGRES=voice-postgres
TEMPORAL_VERSION=1.32.1
UI_VERSION=2.55.0
NAMESPACE=voice-agent-lab
[ -s .env ] || { echo "Run from the source directory, next to .env." >&2; exit 1; }
umask 077

# Settings, added to .env once. The app and the worker reach Temporal by container name.
grep -q '^TEMPORAL_DB_PASSWORD=' .env || echo "TEMPORAL_DB_PASSWORD=$(openssl rand -hex 32)" >> .env
grep -q '^TEMPORAL_ADDRESS=' .env || echo 'TEMPORAL_ADDRESS=temporal:7233' >> .env
grep -q '^TEMPORAL_NAMESPACE=' .env || echo "TEMPORAL_NAMESPACE=$NAMESPACE" >> .env
password=$(grep '^TEMPORAL_DB_PASSWORD=' .env | cut -d= -f2-)
secrets=$(mktemp)
trap 'rm -f "$secrets"' EXIT

# 1. A `temporal` role owning two databases in the existing Postgres. The password
#    goes through stdin, never a command line.
$DOCKER exec -i "$POSTGRES" psql -q -v ON_ERROR_STOP=1 -U voice_agent -d voice_agent <<SQL
SELECT 'CREATE ROLE temporal LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'temporal') \gexec
ALTER ROLE temporal PASSWORD '$password';
SELECT 'CREATE DATABASE temporal OWNER temporal'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'temporal') \gexec
SELECT 'CREATE DATABASE temporal_visibility OWNER temporal'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'temporal_visibility') \gexec
SQL

# 2. Temporal's tables, created or migrated to this server version.
echo "SQL_PASSWORD=$password" > "$secrets"
for db in temporal visibility; do
  dbname=$([ "$db" = temporal ] && echo temporal || echo temporal_visibility)
  $DOCKER run --rm --network "$NETWORK" --env-file "$secrets" --entrypoint sh \
    "temporalio/admin-tools:$TEMPORAL_VERSION" -c "
      set -e
      tool='temporal-sql-tool --plugin postgres12 --ep $POSTGRES -p 5432 -u temporal --db $dbname'
      \$tool setup-schema -v 0.0 >/dev/null 2>&1 || true  # Already set up: harmless.
      \$tool update-schema -d /etc/temporal/schema/postgresql/v12/$db/versioned 2>&1 | tail -1"
done

# 3. The server: one process, all services. Port 7233 on the VM's loopback only, for the CLI.
echo "POSTGRES_PWD=$password" > "$secrets"
$DOCKER rm -f temporal temporal-ui >/dev/null 2>&1 || true
$DOCKER run -d --name temporal --restart unless-stopped --network "$NETWORK" \
  -p 127.0.0.1:7233:7233 --env-file "$secrets" \
  -v "$(pwd)/deploy/temporal-dynamicconfig.yaml:/etc/temporal/dynamicconfig.yaml:ro" \
  -e DYNAMIC_CONFIG_FILE_PATH=/etc/temporal/dynamicconfig.yaml \
  -e DB=postgres12 -e POSTGRES_SEEDS="$POSTGRES" -e DB_PORT=5432 -e POSTGRES_USER=temporal \
  -e BIND_ON_IP=0.0.0.0 -e SQL_MAX_CONNS=10 -e SQL_MAX_IDLE_CONNS=2 -e LOG_LEVEL=warn \
  "temporalio/server:$TEMPORAL_VERSION"
rm -f "$secrets"

echo "Waiting for Temporal..."
admin="$DOCKER run --rm --network $NETWORK temporalio/admin-tools:$TEMPORAL_VERSION temporal --address temporal:7233"
healthy=
for _ in $(seq 30); do
  $admin operator cluster health >/dev/null 2>&1 && { healthy=1; break; }
  sleep 2
done
[ -n "$healthy" ] || { echo "Temporal did not start: $DOCKER logs temporal" >&2; exit 1; }
$admin operator namespace describe -n "$NAMESPACE" >/dev/null 2>&1 \
  || $admin operator namespace create -n "$NAMESPACE" --retention 30d

# 4. The web UI, on the VM's loopback only: view it through an SSH tunnel (docs/deploy.md).
$DOCKER run -d --name temporal-ui --restart unless-stopped --network "$NETWORK" \
  -p 127.0.0.1:8233:8080 -e TEMPORAL_ADDRESS=temporal:7233 "temporalio/ui:$UI_VERSION"

# 5. The callback worker: the app's image, another command, no health endpoint.
$DOCKER rm -f voice-callbacks >/dev/null 2>&1 || true
$DOCKER run -d --name voice-callbacks --restart unless-stopped --network "$NETWORK" \
  --env-file .env --no-healthcheck voice-agent-lab python -m app.callback_worker
echo "Temporal ready. Restart the app container so it reads TEMPORAL_ADDRESS from .env."
