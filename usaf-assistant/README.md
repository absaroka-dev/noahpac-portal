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
| Tests | `tests/test_usaf_assistant.py`, `tests/test_usaf_library_update.py` (staging, review, gate, swap and restore; no network), `tests/test_usaf_assistant_api.py` (needs the venv: `/opt/noahpac/clinical-assistant/venv/bin/python3 -m unittest ...`) |

## Updating the library
**Publications that e-Publishing changed (the normal case): staged, reviewed, gated.** The daily check downloads a changed file into `/var/lib/noahpac-usaf-assistant/staging/<file>/` and raises "Newer Publication Available" for that document; it never touches the library or the index. Then:
1. `sudo ops/usaf-assistant-update.sh review "DAFI 31-117"`: pages, sections added/removed/changed, a text diff, and the dates on the cover pages. Read it: a "newer file" can be a guidance memorandum added at the front (DAFGM), a corrected re-issue or a rewrite.
2. `sudo ops/usaf-assistant-update.sh apply "DAFI 31-117" --version "DAFI 31-117, <edition> (<what changed>)" --pub-date YYYY-MM-DD --dry-run` builds a complete candidate library and index in a separate directory (a full ingest, a few minutes, Voyage embedding cost of roughly ten cents; no OCR is needed for these text PDFs) and evaluates it: structure against the old version, 25 self-retrieval probes (passages of the old version must be found in the new index), and optional golden questions (`usaf-assistant/eval/golden.json`). The gate refuses a file that lost most of its text, sections or retrievability.
3. Without `--dry-run` it asks for "yes", backs up the library file, manifest, report and index (`backups/`, newest 3 kept per publication), stops the API, swaps the index and the file in, starts the API, checks it, and restores the backup by itself if anything fails. It records `last_verified`, deletes the staged copy and re-runs the currency check, which clears the alert.
4. `sudo ops/usaf-assistant-update.sh rollback "DAFI 31-117"` puts the last backup back. `list` shows what is staged. Everything is appended to `updates.log` in the state directory. Without `--version` the page says the edition date is not confirmed; give the edition from the cover.
Only publications with a `check_url` can be staged (DAFI 31-117, DAFI 36-2910, DAFMAN 48-123, DoDM 5210.42/DAFMAN 13-501). The CAC-only ones (MSD, DAFECD, DAFOCD, NARSUM template) are still loaded by hand:

1. Copy the new PDF to `/var/lib/noahpac-usaf-assistant/library/` and add/adjust its entry in `manifest.json` (short name, publication date, category; `"parser": "msd"` for the Medical Standards Directory).
2. `sudo /var/www/noahpac-portal/ops/usaf-assistant-ingest.sh --dry-run` (check page and chunk counts), then without `--dry-run`, then `sudo systemctl restart usaf-assistant-api`.
3. A standard is cited as the MSD asks: "MSD, 5 Jun 2026, C60".

The page shows the list of loaded publications with their versions (`/api/library`, built from `ingest_report.json` plus `manifest.json`; the `version` and `note` fields in the manifest are what the user sees). Text documents (`.txt`) can be loaded like PDFs; `modes/narsum_template.txt` is the NARSUM layout used by the NARSUM mode.

## Are the publications still current? (`check_library_currency.py`)
`usaf-assistant-currency.timer` (daily, about 06:40) runs `ops/usaf-assistant-currency.sh`. Two kinds of check, set per publication in `manifest.json`:
- **`check_url`** (public e-Publishing PDF): the file is fetched in a real headless browser (`fetch-epub.mjs`; e-Publishing's bot protection answers scripts with 403) and compared byte for byte with the loaded copy. Same = `current`; different = `update_available`.
- **No URL** (Medical Standards Directory, classification directories: CAC only): `last_verified` + `verify_every_days`. When the date passes the page shows `verify_due`. After confirming on the official site that the loaded edition is the newest: `sudo ops/usaf-assistant-currency.sh --mark-verified DAFECD DAFOCD MSD` (short names or file names).
A changed file is also **staged** (`staging/<file>/<sha12>-<file>` + `meta.json`: URL, hash, e-Publishing modification time, hash of the loaded copy); metric `usaf_library_doc_staged{doc}`. A fetch that fails keeps what is already staged.
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
