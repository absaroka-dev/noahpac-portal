#!/bin/bash
# Run as root in your own terminal. Replaces ONLY the Voyage key of the clinical assistant (hidden prompt), after testing that the new key works.
set -e
ENV=/etc/clinical-assistant-api.env
VENV=/opt/noahpac/clinical-assistant/venv/bin/python3
read -rsp "New Voyage API key for the clinical assistant: " V; echo
[ -n "$V" ] || { echo "Empty key; nothing changed."; exit 1; }
VOYAGE_API_KEY="$V" $VENV - <<'PY' || { echo "That key was rejected by Voyage; nothing changed."; exit 1; }
import os, voyageai
voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"]).embed(["test"], model="voyage-3", input_type="query")
PY
cp -p "$ENV" "/root/clinical-assistant-api.env.before-$(date +%Y%m%d%H%M%S)"
tmp=$(mktemp); chmod 600 "$tmp"
grep -v '^export VOYAGE_API_KEY=' "$ENV" > "$tmp"
printf 'export VOYAGE_API_KEY=%q\n' "$V" >> "$tmp"
cat "$tmp" > "$ENV"; rm -f "$tmp"; chown root:www-data "$ENV"; chmod 640 "$ENV"
systemctl restart clinical-assistant-api
sleep 5
curl -fs http://127.0.0.1:8766/health && echo && echo "clinical assistant restarted with the new Voyage key."
