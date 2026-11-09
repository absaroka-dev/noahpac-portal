# Working on noahpac-portal (humans, Codex, Claude Code)

Read this before changing anything. Two AI assistants and the owner work in this repository; these rules keep them from stepping on each other.
`CLAUDE.md` points here. Server-wide documentation lives in the separate `noahflix-server` repo (`SERVER_README.md` there); this file covers only this project.

## The one thing to remember
`/var/www/noahpac-portal` is **the live web root and the working checkout at the same time**. Nginx serves it directly. Whatever is saved in a tracked
file there is public immediately, and `scripts/deploy-production.sh` refuses to deploy while tracked files have uncommitted edits.

## Change flow (same for everyone)
1. Work on a branch (a `git worktree` in `/tmp` keeps the live checkout untouched), open a pull request, wait for both CI checks (CSS tokens, static integrity and browser regression).
2. A substantive clinical, safety or governance change needs an entry in `clinical-changelog.json` (newest first). The updates page renders every entry and needs all fields,
   including `"source_ids": []` when there are none; a missing field breaks the page and its Playwright test.
3. Merge to `main`, then deploy that exact commit once: `/opt/scripts/deploy-noahpac.sh <full-sha>` (wraps `./scripts/deploy-production.sh`). Whoever merges deploys, and says so.
4. Never leave half-finished edits to tracked files in the live checkout. Never run `git checkout`, `git stash` or `git clean` on it: runtime files (`*/last_checked.json`, screener data,
   `clinical-assistant/` state, `replit.md`) are rewritten by monitors and must be preserved (see `docs/RELEASE_RUNBOOK.md`).
5. Git commit author name is currently the old GitHub name `jamjop` in this checkout's config; new work should use `absaroka-dev` (pass `-c user.name=absaroka-dev`).

Files several people edit often (pull first, keep changes small, expect conflicts): `index.html`, `clinical-changelog.json`, `scripts/check-site-health.mjs`, `shared.css`.
A new tool or private route needs: an entry in `ROUTES` (and `PRIVATE_ROUTES` if behind Authelia) and, if its page is not at `<route>/index.html`, a case in `localPathForUrl`/`checkLocal` in
`scripts/check-site-health.mjs`.

## Private tools (Authelia two-factor; everything else on the domain is public)
`/clinical-assistant/`, `/review/` and `/usaf-assistant/` (Med Standards Assistant). Do not remove an `auth_request` from their nginx locations, and do not link them from anywhere that implies they are public.

## Lives OUTSIDE this repository (do not delete, "clean up" or regenerate)
- nginx vhost `/etc/nginx/conf.d/noahpac.com.conf` (tracked copy in the `noahflix-server` repo, `nginx/noahpac.com.conf`; `ops/nginx/*.conf` here are the source snippets). Test with `nginx -t` before reload.
- Authelia rules and groups: `/opt/stacks/authelia/config/` (rule for `^/usaf-assistant` needs group `usaf-assistant`).
- systemd units installed from `ops/systemd/` into `/etc/systemd/system/`; API keys in `/etc/clinical-assistant-api.env` and `/etc/usaf-assistant-api.env` (never print, log, commit or paste them; they are set with the
  `ops/*-set-*key*.sh` scripts, which prompt with hidden input).
- Runtime state: `/var/lib/noahpac-clinical-assistant/` and `/var/lib/noahpac-usaf-assistant/` (library PDFs, `manifest.json`, `chroma_data`, `audit.jsonl`, usage counters). The search index is rebuilt from the PDFs; the PDFs themselves are not in git.
- Ports in use on the host: 8766 clinical assistant, 8767 review portal, 8768 Med Standards Assistant.

## Med Standards Assistant (`usaf-assistant/`)
Private drafting aid for USAF NARSUMs, medical standards checks and waiver packets, answering only from loaded publications. Read `usaf-assistant/README.md`. Rules that matter:
- Input is **de-identified only**; the safety filters (`safety_usaf.py`) and tests must keep refusing SSN, DoD ID, birth dates and rank-plus-name. Do not weaken them, and never add logging of question or answer text (the audit log stores a hash and length only).
- Answers must stay grounded: every answer cites retrieved excerpts, and tags and paragraph numbers are validated (`refcheck.py`). The Medical Standards Directory is parsed as a table (`msd_tables.py`); a plain-text reader loses which column an X is under.
- Any API call that can take more than about 5 seconds must be a background job with polling: **Cloudflare cuts requests whose first byte takes longer than about 5 s** (both zones). `/api/ask` starts a job, the page polls `/api/result/<id>`.
- Tests: `python3 -m unittest tests.test_usaf_assistant`; API flow with fake clients needs the venv: `/opt/noahpac/clinical-assistant/venv/bin/python3 -m unittest tests.test_usaf_assistant_api`.
- Updating the library: see the README (copy the PDF, edit `manifest.json` with the version shown on the page, run `ops/usaf-assistant-ingest.sh`, restart the service). Publications are public documents; do not add restricted or personal material.

## Other things learned the hard way
- A new clinical-assistant style API belongs on its own port, systemd unit and env file; do not reuse another tool's key.
- `ReadWritePaths` in the unit files is deliberate: the services run as `www-data`, sandboxed, and can only write their own state directory.
- Slow pages on both noahpac.com and noahflix.net can simply be a Cloudflare incident: compare a request straight to the origin (about 7 ms) before changing anything.
