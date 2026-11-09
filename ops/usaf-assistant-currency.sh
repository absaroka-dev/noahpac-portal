#!/bin/bash
# Med Standards Assistant: are the loaded publications still current?  (daily timer; also run by hand)
#   sudo ops/usaf-assistant-currency.sh                       run all checks now
#   sudo ops/usaf-assistant-currency.sh --mark-verified DAFECD DAFOCD   record that you just confirmed those are the newest versions
set -e
export USAF_ASSISTANT_STATE_DIR=/var/lib/noahpac-usaf-assistant USAF_ASSISTANT_LIBRARY_DIR=/var/lib/noahpac-usaf-assistant/library
export HOME=${HOME:-/root} PLAYWRIGHT_TEST_DIR=/var/www/noahpac-portal/tests
cd /var/www/noahpac-portal/usaf-assistant
python3 check_library_currency.py "$@"
chown www-data:www-data /var/lib/noahpac-usaf-assistant/currency.json 2>/dev/null || true
