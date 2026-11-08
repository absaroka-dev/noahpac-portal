#!/bin/bash
# Run as root in your own terminal. Writes /etc/usaf-assistant-api.env (root:www-data, mode 640) from two hidden prompts.
# Use an Anthropic key created for this assistant only (so it has its own spend limit in the Anthropic console).
set -e
umask 077
read -rsp "Anthropic API key for the USAF assistant: " A; echo
read -rsp "Voyage AI API key: " V; echo
[ -n "$A" ] && [ -n "$V" ] || { echo "Both keys are required; nothing saved."; exit 1; }
{
  printf 'export ANTHROPIC_API_KEY=%q\n' "$A"
  printf 'export VOYAGE_API_KEY=%q\n' "$V"
  printf 'export USAF_EMBED_DELAY=%q\n' "${USAF_EMBED_DELAY:-30}"
} > /etc/usaf-assistant-api.env
chown root:www-data /etc/usaf-assistant-api.env; chmod 640 /etc/usaf-assistant-api.env
echo "saved /etc/usaf-assistant-api.env (root:www-data 640)."
