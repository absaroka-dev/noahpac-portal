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

## Are the publications still current? (`check_library_currency.py`)
`usaf-assistant-currency.timer` (daily, about 06:40) runs `ops/usaf-assistant-currency.sh`. Two kinds of check, set per publication in `manifest.json`:
- **`check_url`** (public e-Publishing PDF): the file is fetched in a real headless browser (`fetch-epub.mjs`; e-Publishing's bot protection answers scripts with 403) and compared byte for byte with the loaded copy. Same = `current`; different = `update_available`.
- **No URL** (Medical Standards Directory, classification directories: CAC only): `last_verified` + `verify_every_days`. When the date passes the page shows `verify_due`. After confirming on the official site that the loaded edition is the newest: `sudo ops/usaf-assistant-currency.sh --mark-verified DAFECD DAFOCD MSD` (short names or file names).
Results: `currency.json` in the state directory (shown on the page), textfile metrics `usaf_library_*` and four Grafana alerts (`usaf-library-currency.yaml` in the monitoring stack). A publication that is `current` no longer shows the generic "older than 540 days" warning. Needs Chromium from Playwright (`PLAYWRIGHT_CHROMIUM` overrides the search) and `tests/node_modules/@playwright/test`.
Loaded from e-Publishing on 2026-11-08 and monitored: DAFI 31-117, DoDM 5210.42/DAFMAN 13-501 (GM), DAFMAN 48-123, DAFI 36-2910 (GM). Not on e-Publishing (CAC): MSD, DAFECD, DAFOCD, NARSUM template.

## Identifiers are replaced, not just refused
`safety_usaf.redact()` replaces SSN, DoD ID, phone, email, street address, labelled birth dates and record numbers, and names after a rank/title or a label ("Name:", "named") with placeholders (`[SSN]`, `[NAME]`...). `POST /api/ask` then answers `409 {needs_confirmation, clean_question, findings}` and sends nothing on; the page puts the cleaned text back in the input for the person to check and send again. Earlier turns are cleaned silently. Only counts are logged (`redaction_offered`), never text. Exact calendar dates are not replaced (warning only) and dates inside publication citations ("MSD, 5 Jun 2026, C60") are untouched. Names that follow no rank or label, units and unusual details cannot be found by rules: list known words (bases, units, people) in `/etc/usaf-assistant/redact-terms.txt` (see `redact-terms.example.txt`); `detected_identifier()` stays as the final gate.

## Adding a person
Add `usaf-assistant` to the user's `groups` in `/opt/stacks/authelia/config/users_database.yml`, restart the `authelia` container.

## Limits and honest caveats
- Daily per-user caps (`USAF_DAILY_CALLS` 60, `USAF_DAILY_TOKENS` 300000), 10 requests/minute per IP at nginx.
- A document that is out of date is flagged when its manifest date is older than 540 days; the tool cannot know whether e-Publishing has a newer one.
- Drafting aid only; nothing is submitted anywhere.
