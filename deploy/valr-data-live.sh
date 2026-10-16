#!/bin/sh
# The worker's writable VALR data (docs/deploy.md, Interview assistant > Worker data).
#
# /srv/valr-data (root:1000, 0750) stays read-only at /data in both containers. Files the
# research worker rewrites after each interview (performance.json, performance_scores.csv)
# live in /srv/valr-data-live (owned by the image's user, uid 1000, 0750), mounted:
#   voice-research:  -v /srv/valr-data-live:/data-live      (read-write)
#   voice-agent-lab: -v /srv/valr-data-live:/data-live:ro   (the pages read the same file)
# with VALR_PERFORMANCE_PATH / VALR_PERFORMANCE_CSV pointing there, and, in the worker,
# VALR_PERFORMANCE_FALLBACK=/data/performance.json (read while the live file does not exist).
#
#   sudo sh deploy/valr-data-live.sh setup   # create the dir, seed it (cp -n): idempotent
#   sh deploy/valr-data-live.sh app          # prints the docker run options for the app
#   sh deploy/valr-data-live.sh worker       # ... for voice-research
# `app`/`worker` print the old read-only /data mount only, when the live dir does not exist.
set -eu
RO=${VALR_DATA_RO:-/srv/valr-data}
LIVE=${VALR_DATA_LIVE:-/srv/valr-data-live}
APP_UID=${VALR_DATA_UID:-1000}  # Dockerfile: useradd --uid 1000 app; USER app (both targets)
ENV_LIVE="-e VALR_PERFORMANCE_PATH=/data-live/performance.json -e VALR_PERFORMANCE_CSV=/data-live/performance_scores.csv"

case "${1:-}" in
  setup)
    install -d -o "$APP_UID" -g "$APP_UID" -m 0750 "$LIVE"
    for f in performance.json performance_scores.csv; do
      if [ -f "$RO/$f" ] && [ ! -e "$LIVE/$f" ]; then
        cp -n "$RO/$f" "$LIVE/$f"
        chown "$APP_UID:$APP_UID" "$LIVE/$f"; chmod 0600 "$LIVE/$f"
        echo "seeded $LIVE/$f"
      fi
    done
    echo "$LIVE: $(ls -ld "$LIVE" | awk '{print $1, $3":"$4}'), $(ls "$LIVE" | wc -l | tr -d " ") file(s)"
    ;;
  app|worker)
    ro=""; [ -d "$RO" ] && ro="-v $RO:/data:ro"
    if [ ! -d "$LIVE" ]; then echo "$ro"; exit 0; fi
    if [ "$1" = app ]; then
      echo "$ro -v $LIVE:/data-live:ro $ENV_LIVE"
    else
      echo "$ro -v $LIVE:/data-live $ENV_LIVE -e VALR_PERFORMANCE_FALLBACK=/data/performance.json"
    fi
    ;;
  *) echo "usage: sh deploy/valr-data-live.sh setup|app|worker" >&2; exit 2 ;;
esac
