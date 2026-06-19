# Video Auto-Poster

A scheduled worker that watches one Dropbox folder for new video files and, for
each new video: generates engagement-optimized YouTube metadata (including a
snarky Alliance jab + a subscribe/website CTA), optionally lets you approve it,
uploads the video to YouTube, then moves the source file to a `/posted` folder.

> **YouTube-only.** Facebook posting was removed (it was suppressing the FB
> algorithm). Photo handling is dormant: photos are recognized and set aside
> cleanly (no destination), so a new photo target can be wired in later.

---

## Design in one screen

- **Not a daemon, not Claude Code.** It's plain Python run by **cron**. Each
  invocation does one pass and exits.
- **Two stages, two commands, with a human approval gate between them:**
  1. `python main.py` — find new videos → generate metadata → write a pending
     review file (or, in `auto_publish` mode, publish immediately).
  2. `python publish.py <pending-id>` — take an **approved** pending item →
     upload to YouTube → move the Dropbox source.
- **Only one LLM call:** metadata generation (Anthropic). Everything else
  (Dropbox, YouTube) is deterministic code.
- **Move semantics:** source → `/posted` **only after the upload succeeds**;
  otherwise → `/failed` + notify. A dormant media type (e.g. a photo, with no
  destination) is a clean SKIP — recorded, source left in place. A processed
  file is never left in the watch folder and never silently lost.
- **Fail loud:** rotating log file, one-line stdout run summary, non-zero exit on
  failure.
- **`--dry-run`** does everything except the real upload and the move.
- **Safety defaults:** YouTube `privacyStatus` is config-driven.

---

## Project layout

```
.env / .env.example      # secrets (.env is gitignored; .env.example documents every key)
.gitignore
requirements.txt
config.yaml              # non-secret settings
config.py                # loads .env + config.yaml -> one typed Settings object   [built]
notify.py                # notify() hook (print+log now; email/Slack later)         [stub]
dropbox_client.py        # auth, list-new (cursor), download, move                  [stub]
metadata.py              # Anthropic call -> validated JSON metadata                 [stub]
youtube_auth.py          # one-time: mint a YouTube OAuth refresh token
youtube_upload.py        # resumable upload + custom thumbnail using the refresh token
thumbnail.py             # branded custom YouTube thumbnail (sharpest frame + hook)
state/
  pending/               # pending review files written by main.py
  processed/             # result records written by publish.py
  (cursor)               # stored Dropbox cursor (created at runtime)
main.py                  # STAGE 1: poll -> metadata -> pending -> stop             [stub]
publish.py               # STAGE 2: approve -> upload both -> move source           [stub]
README.md
CREDENTIALS.md           # exact steps to obtain every secret
```

---

## Setup

```bash
# 1. Python 3.11+ and a virtualenv
python3 -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure secrets
cp .env.example .env
#    ...then follow CREDENTIALS.md to fill in every key.

# 4. Verify config loads (prints non-secret settings; never prints secret values)
python config.py
```

---

## Credentials

All secrets come from `.env`. See **[CREDENTIALS.md](CREDENTIALS.md)** for exact
step-by-step instructions for Anthropic, Dropbox, and YouTube — including the
gotcha that will bite later:
- YouTube: the OAuth consent screen **must be "In production"** or the refresh
  token expires in 7 days.

---

## Running (once built)

```bash
# Stage 1 — find new videos, generate metadata, write pending review files
python main.py
python main.py --dry-run        # do everything except write pending records

# Review state/pending/<id>.json, mark it approved.

# Stage 2 — publish an approved item
python publish.py <pending-id>
python publish.py <pending-id> --dry-run   # everything except real upload + move
```

### Cron (later)
`main.py` is the only thing cron runs (it never publishes). Example — every
30 minutes:
```cron
*/30 * * * * cd /path/to/video-autoposter && .venv/bin/python main.py >> logs/cron.log 2>&1
```
Publishing stays manual: you review the pending file, then run `publish.py`.

---

## Status

Live on GitHub Actions (cron every 30 min + manual `workflow_dispatch`),
YouTube-only, in `auto_publish` mode. Pipeline modules:
- `config.py` + `notify.py` — typed config + centralized logging
- `dropbox_client.py` — list-new (cursor), download, move, sidecar notes
- `metadata.py` — the single Anthropic call → validated, engagement-tuned metadata
- `main.py` (Stage 1) / `publish.py` (Stage 2)
- `youtube_auth.py` / `youtube_upload.py` / `thumbnail.py` — upload + branded thumbnail

> Facebook removed 2026-06-19 (it was suppressing the FB algorithm). If you ever
> add a new destination (e.g. Instagram for photos), wire it into
> `publish._enabled_targets` and the publish loop.
