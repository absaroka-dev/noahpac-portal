#!/bin/bash
# Med Standards Assistant: review and load a newer publication that the daily currency check has staged.
#   sudo ops/usaf-assistant-update.sh list
#   sudo ops/usaf-assistant-update.sh review "DAFI 31-117"
#   sudo ops/usaf-assistant-update.sh apply  "DAFI 31-117" --version "DAFI 31-117, 13 Nov 2026" --pub-date 2026-11-13 [--dry-run]
#   sudo ops/usaf-assistant-update.sh rollback "DAFI 31-117"
# apply builds a candidate index in a separate directory, checks it, and only then swaps it in (with backup and automatic restore).
set -e
export USAF_ASSISTANT_STATE_DIR=${USAF_ASSISTANT_STATE_DIR:-/var/lib/noahpac-usaf-assistant}
export USAF_ASSISTANT_LIBRARY_DIR=${USAF_ASSISTANT_LIBRARY_DIR:-$USAF_ASSISTANT_STATE_DIR/library}
cd "$(dirname "$(readlink -f "$0")")/../usaf-assistant"
exec /opt/noahpac/clinical-assistant/venv/bin/python3 library_update.py "$@"
