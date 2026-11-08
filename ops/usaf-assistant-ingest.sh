#!/bin/bash
# Rebuild the USAF assistant index from /var/lib/noahpac-usaf-assistant/library (run as root). Add --dry-run to only check parsing.
set -e
exec runuser -u www-data -- bash -c '
  set -a; source /etc/usaf-assistant-api.env; set +a
  export USAF_ASSISTANT_STATE_DIR=/var/lib/noahpac-usaf-assistant USAF_ASSISTANT_LIBRARY_DIR=/var/lib/noahpac-usaf-assistant/library
  cd /var/www/noahpac-portal/usaf-assistant
  exec /opt/noahpac/clinical-assistant/venv/bin/python3 ingest_usaf.py "$@"' _ "$@"
