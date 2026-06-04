# Video Auto-Poster

A scheduled worker that watches one Dropbox folder for new video files and, for
each new video: generates posting metadata, lets you approve it, uploads the
video to YouTube and to a Facebook Page, then moves the source file to a
`/posted` folder.

> **Status: scaffold only (Step 1).** The plumbing modules are stubs — we build
> and test them one at a time in later steps. Nothing publishes yet.

---

## Design in one screen

- **Not a daemon, not Claude Code.** It's plain Python run by **cron**. Each
  invocation does one pass and exits.
- **Two stages, two commands, with a human approval gate between them:**
  1. `python main.py` — find new videos → generate metadata → write a pending
     review file → **STOP**. Never publishes.
  2. `python publish.py <pending-id>` — take an **approved** pending item →
     upload to YouTube **and** Facebook → move the Dropbox source.
- **Only one LLM call:** metadata generation (Anthropic). Everything else
  (Dropbox, YouTube, Facebook) is deterministic code.
- **Move semantics:** source → `/posted` **only after BOTH uploads succeed**;
  otherwise → `/failed` + notify. A processed file is never left in the watch
  folder and never silently lost.
- **Fail loud:** rotating log file, one-line stdout run summary, non-zero exit on
  failure.
- **`--dry-run`** does everything except the real uploads and the move.
- **Safety defaults:** YouTube `privacyStatus` defaults to `unlisted`
  (config-driven). The Graph API version is pinned (`v25.0`) in every Facebook
  call.

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
youtube_auth.py          # one-time: mint a YouTube OAuth refresh token             [stub]
youtube_upload.py        # resumable upload using the refresh token                 [stub]
facebook_upload.py       # post video to a Page (Graph version pinned)              [stub]
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
step-by-step instructions for Anthropic, Dropbox, YouTube, and Facebook —
including two gotchas that will bite later:
- YouTube: the OAuth consent screen **must be "In production"** or the refresh
  token expires in 7 days.
- Facebook: **App Review + Business Verification** are required before production
  use beyond your own Pages.

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

## Build order (what comes next)

We build and test one module at a time, in this order:
1. ✅ Scaffold
2. ✅ `config.py` + `notify.py` (notify + centralized logging)
3. ✅ `dropbox_client.py` (list-new with cursor, download, move, sidecar) — live-tested
4. ✅ `metadata.py` (Anthropic → validated JSON) — live-tested
5. ✅ `dropbox_auth.py` (mint Dropbox refresh token) — used
6. ✅ `main.py` (Stage 1: poll → metadata → pending) — live-tested
7. ✅ `youtube_auth.py` / `youtube_upload.py` / `facebook_upload.py` — built; need creds to live-test
8. ✅ `publish.py` (Stage 2) — gates + dry-run tested; upload path needs YouTube/Facebook creds

Remaining to go fully live:
- Provision **YouTube** creds (Google Cloud project → Desktop OAuth client → `python youtube_auth.py`)
- Provision **Facebook** creds (Meta app → long-lived Page token)
- One real end-to-end publish
