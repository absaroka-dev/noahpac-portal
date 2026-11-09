# Med Standards Assistant

Private tool at `https://www.noahpac.com/usaf-assistant/` (Authelia 2FA, group `usaf-assistant`). Standards checks, NARSUM drafts and waiver-packet
help answered only from publications loaded in the library. Input must be de-identified (names, SSN, DoD ID, birth dates, addresses are refused).

| Piece | Where |
|---|---|
| Page (static) | `usaf-assistant/web/` (the only directory nginx serves) |
| API | `api_server.py`, `127.0.0.1:8768`, `usaf-assistant-api.service` (`ops/systemd/`), user from the `X-Remote-User` header nginx sets from the Authelia session |
| Index builder | `ingest_usaf.py` (run through `ops/usaf-assistant-ingest.sh`); table parser for the MSD in `msd_tables.py`; text chunker `textproc.py` |
| Retrieval | Voyage `voyage-3` vectors + BM25 (`retrieval.py`), fused; abbreviations in `glossary.json` |
| Citations | model cites `[S#]` tags; tags and quoted paragraph/table numbers are checked against the retrieved text (`refcheck.py`) |
| State (not in git) | `/var/lib/noahpac-usaf-assistant/` : `library/` (PDFs + `manifest.json`), `chroma_data/`, `audit.jsonl` (no question text), `usage.json` (daily caps), `ingest_report.json` |
| Keys | `/etc/usaf-assistant-api.env` (root:www-data 640), written by `ops/usaf-assistant-set-keys.sh` |
| nginx | `ops/nginx/noahpac-usaf-assistant.locations.conf` (pasted into `/etc/nginx/conf.d/noahpac.com.conf`) |
| Tests | `tests/test_usaf_assistant.py`, `tests/test_usaf_assistant_api.py` (needs the venv: `/opt/noahpac/clinical-assistant/venv/bin/python3 -m unittest ...`) |

## Updating the library
1. Copy the new PDF to `/var/lib/noahpac-usaf-assistant/library/` and add/adjust its entry in `manifest.json` (short name, publication date, category; `"parser": "msd"` for the Medical Standards Directory).
2. `sudo /var/www/noahpac-portal/ops/usaf-assistant-ingest.sh --dry-run` (check page and chunk counts), then without `--dry-run`, then `sudo systemctl restart usaf-assistant-api`.
3. A standard is cited as the MSD asks: "MSD, 5 Jun 2026, C60".

The page shows the list of loaded publications with their versions (`/api/library`, built from `ingest_report.json` plus `manifest.json`; the `version` and `note` fields in the manifest are what the user sees). Text documents (`.txt`) can be loaded like PDFs; `modes/narsum_template.txt` is the NARSUM layout used by the NARSUM mode.

## Adding a person
Add `usaf-assistant` to the user's `groups` in `/opt/stacks/authelia/config/users_database.yml`, restart the `authelia` container.

## Limits and honest caveats
- Daily per-user caps (`USAF_DAILY_CALLS` 60, `USAF_DAILY_TOKENS` 300000), 10 requests/minute per IP at nginx.
- A document that is out of date is flagged when its manifest date is older than 540 days; the tool cannot know whether e-Publishing has a newer one.
- Drafting aid only; nothing is submitted anywhere.
